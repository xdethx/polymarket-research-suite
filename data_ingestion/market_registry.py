"""
market_registry.py — FAZ 2.1
Aktif Polymarket market'lerini takip eder.

Sorumluluklar:
  - Periyodik olarak aktif market'leri Gamma API'den çeker
  - Yeni market açılınca DataBus'a token kayıt eder, PTB çeker
  - market_resolved eventi gelince session'ı kapatır
  - Her market için seconds_remaining'i günceller
  - new_market WS eventi gelince polling'i destekler (hybrid yaklaşım)

Hybrid yaklaşım neden:
  new_market eventi gelmeyebilir (bağlantı gecikmesi, restart vb.)
  Polling fallback olarak her 30sn çalışır — kaçan market olmaz.

Kullanım:
    registry = MarketRegistry(bus, symbols=["BTC","ETH","SOL","XRP"], intervals=[5,15])
    registry.start()   # arka planda çalışır (daemon thread)
"""

import time
import json
import threading
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from data_bus import DataBus

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"
POLY_PRICE_BASE = "https://polymarket.com/api/crypto/crypto-price"

VARIANT_MAP = {5: "fiveminute", 15: "fifteen"}

# Gamma polling aralığı — yeni market kontrolü
POLL_INTERVAL = 10   # saniye — yeni periyotları daha erken yakala

# PTB retry
PTB_RETRIES = 3
PTB_RETRY_DELAY = 2  # saniye

# Stale-session fallback: end_date'i bu kadar saniye geçmiş hâlâ açık session'lar
# (WS market_resolved eventi kaçırıldı) poll döngüsünde zorla kapatılır.
STALE_CLOSE_GRACE = 120  # saniye


class MarketSession:
    """Tek bir market periyodunun state'i."""

    __slots__ = (
        "slug", "symbol", "interval", "market_condition_id",
        "yes_token_id", "no_token_id", "end_date", "ptb",
        "opened_at", "snapshots",
    )

    def __init__(
        self,
        slug:                 str,
        symbol:               str,
        interval:             int,
        market_condition_id:  str,
        yes_token_id:         str,
        no_token_id:          str,
        end_date:             str,
        ptb:                  float,
    ):
        self.slug                = slug
        self.symbol              = symbol
        self.interval            = interval
        self.market_condition_id = market_condition_id
        self.yes_token_id        = yes_token_id
        self.no_token_id         = no_token_id
        self.end_date            = end_date
        self.ptb                 = ptb
        self.opened_at           = time.time()
        self.snapshots: list     = []   # recorder tarafından doldurulur


class MarketRegistry:
    """
    Aktif market'leri yönetir, DataBus ile senkronize eder.

    slug → MarketSession eşlemesi tutar.
    DataBus'a lifecycle callback'leri verir.
    """

    def __init__(
        self,
        bus:       DataBus,
        symbols:   Optional[list[str]] = None,
        intervals: Optional[list[int]] = None,
    ):
        self._bus       = bus
        self._symbols   = [s.upper() for s in (symbols   or ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"])]
        self._intervals = intervals or [5, 15]

        # slug → MarketSession
        self._sessions: dict[str, MarketSession] = {}
        # token_id → slug (hızlı reverse lookup için)
        self._token_to_slug: dict[str, str] = {}
        self._lock = threading.Lock()

        # Lifecycle callback'leri — recorder ve downstream consumers bu hook'ları kullanır
        # on_session_opened(session: MarketSession)
        # on_session_closed(session: MarketSession, outcome: str,
        #                   close_price: float, winning_asset_id: str)
        self._on_session_opened: list = []
        self._on_session_closed: list = []

        # DataBus'a lifecycle callback'lerini bağla
        self._bus.set_lifecycle_callbacks(
            on_new_market      = self._handle_new_market_event,
            on_market_resolved = self._handle_market_resolved_event,
        )

    # ─────────────────────────────────────────────────────────
    # PUBLIC API
    # ─────────────────────────────────────────────────────────

    def set_session_callbacks(
        self,
        on_session_opened: Optional[callable] = None,
        on_session_closed: Optional[callable] = None,
    ) -> None:
        """
        recorder.py ve downstream consumers bu callback'leri kullanır.
        on_session_opened(session: MarketSession)
        on_session_closed(session: MarketSession, outcome: str,
                          close_price: float, winning_asset_id: str)
        Önceki callback'lerin üzerine yazar. Birden fazla subscriber için
        add_session_callback kullanın.
        """
        self._on_session_opened = [on_session_opened] if on_session_opened else []
        self._on_session_closed = [on_session_closed] if on_session_closed else []

    def add_session_callback(
        self,
        on_session_opened: Optional[callable] = None,
        on_session_closed: Optional[callable] = None,
    ) -> None:
        """
        Mevcut callback listesine yeni subscriber ekler (set_session_callbacks'ın
        üzerine yazmaz). Downstream consumers bu metodu kullanır.
        """
        if on_session_opened:
            self._on_session_opened.append(on_session_opened)
        if on_session_closed:
            self._on_session_closed.append(on_session_closed)

    def get_session_by_token(self, token_id: str) -> Optional[MarketSession]:
        with self._lock:
            slug = self._token_to_slug.get(token_id)
            return self._sessions.get(slug) if slug else None

    def get_all_sessions(self) -> list[MarketSession]:
        with self._lock:
            return list(self._sessions.values())

    def start(self) -> None:
        """Polling döngüsünü arka planda başlat."""
        t = threading.Thread(target=self._poll_loop, daemon=True)
        t.start()
        logger.info(f"[REGISTRY] Başladı: {self._symbols} × {self._intervals}dk")

    # ─────────────────────────────────────────────────────────
    # POLLING DÖNGÜSÜ
    # ─────────────────────────────────────────────────────────

    def _poll_loop(self) -> None:
        """
        Her POLL_INTERVAL saniyede aktif market'leri kontrol et.
        WS new_market eventi kaçırılsa bile market açılır.
        """
        while True:
            try:
                self._sync_markets()
                self._update_seconds_remaining()
                self._close_stale_sessions()
            except Exception as e:
                logger.error(f"[REGISTRY] Poll hatası: {e}")
            time.sleep(POLL_INTERVAL)

    def _sync_markets(self) -> None:
        """Gamma API'den aktif market'leri çek, yeni olanları aç."""
        for symbol in self._symbols:
            for interval in self._intervals:
                try:
                    market_info = self._find_market(symbol, interval)
                    if not market_info:
                        continue

                    slug = market_info["slug"]

                    with self._lock:
                        already_open = slug in self._sessions

                    if not already_open:
                        self._open_session(market_info)
                        # PTB istekleri arası bekleme — rate limit koruması
                        # Aynı anda 12 sembol PTB çekmeye çalışınca Polymarket reddediyor
                        time.sleep(1)

                except Exception as e:
                    logger.error(f"[REGISTRY] {symbol} {interval}dk sync hatası: {e}")

    def _update_seconds_remaining(self) -> None:
        """Tüm aktif session'ların sr'sini güncelle."""
        now = datetime.now(timezone.utc)
        with self._lock:
            sessions = list(self._sessions.values())

        for session in sessions:
            try:
                end_dt = datetime.fromisoformat(
                    session.end_date.replace("Z", "+00:00")
                )
                sr = max(0, int((end_dt - now).total_seconds()))
                self._bus.update_seconds_remaining(session.yes_token_id, sr)
            except Exception:
                pass

    # ─────────────────────────────────────────────────────────
    # SESSION AÇMA / KAPATMA
    # ─────────────────────────────────────────────────────────

    def _open_session(self, market_info: dict) -> None:
        """
        Yeni market için session oluştur, PTB çek, DataBus'a kayıt et.
        """
        symbol   = market_info["symbol"]
        interval = market_info["interval"]
        slug     = market_info["slug"]

        # Polymarket yeni periyot açıldığında openPrice'ı hemen finalize etmiyor.
        # Çok erken çekilirse geçici/yanlış değer dönüyor.
        # 5sn bekleyerek API'nin yerleşmesini sağla.
        time.sleep(5)

        # PTB çek — birkaç deneme
        ptb = None
        for attempt in range(PTB_RETRIES):
            ptb = self._fetch_ptb(symbol, market_info)
            if ptb:
                break
            time.sleep(PTB_RETRY_DELAY)

        if not ptb:
            logger.warning(f"[REGISTRY] PTB alınamadı, atlandı: {symbol} {interval}dk")
            return

        session = MarketSession(
            slug                = slug,
            symbol              = symbol,
            interval            = interval,
            market_condition_id = market_info["market_condition_id"],
            yes_token_id        = market_info["yes_token_id"],
            no_token_id         = market_info["no_token_id"],
            end_date            = market_info["end_date"],
            ptb                 = ptb,
        )

        with self._lock:
            # F3 — çift-açılış koruması: poll thread ile WS thread aynı slug için
            # _open_session'a yarışabilir (already_open kontrolü ile bu insert
            # arasında 5–41 sn geçer). Kilit altında son kez kontrol et — slug
            # zaten varsa NOOP: üzerine yazma, callback'leri tekrar tetikleme.
            if slug in self._sessions:
                logger.info(
                    f"[REGISTRY] Session zaten açık — çift-açılış atlandı: {slug}"
                )
                return
            self._sessions[slug]                         = session
            self._token_to_slug[session.yes_token_id]    = slug
            self._token_to_slug[session.no_token_id]     = slug

        # DataBus'a yes_token kayıt et (mid/spread yes_token üzerinden gelir)
        self._bus.register_token(
            token_id          = session.yes_token_id,
            symbol            = symbol,
            interval          = interval,
            ptb               = ptb,
            end_date          = market_info["end_date"],
            seconds_remaining = market_info["seconds_remaining"],
        )

        logger.info(
            f"[REGISTRY] Session açıldı: {symbol} {interval}dk "
            f"| PTB:{ptb:.2f} | slug:{slug}"
        )

        for cb in self._on_session_opened:
            try:
                cb(session)
            except Exception as e:
                logger.error(f"[REGISTRY] on_session_opened hatası: {e}")

    def _close_session(
        self,
        slug:             str,
        outcome:          str,
        close_price:      float,
        winning_asset_id: str,
    ) -> None:
        """Session'ı kapat, DataBus'tan token'ı sil, callback'i çağır."""
        with self._lock:
            session = self._sessions.pop(slug, None)
            if session:
                self._token_to_slug.pop(session.yes_token_id, None)
                self._token_to_slug.pop(session.no_token_id, None)

        if not session:
            return

        self._bus.unregister_token(session.yes_token_id)

        logger.info(
            f"[REGISTRY] Session kapandı: {session.symbol} {session.interval}dk "
            f"→ {outcome} | close:{close_price:.4f}"
        )

        for cb in self._on_session_closed:
            try:
                cb(
                    session          = session,
                    outcome          = outcome,
                    close_price      = close_price,
                    winning_asset_id = winning_asset_id,
                )
            except Exception as e:
                logger.error(f"[REGISTRY] on_session_closed hatası: {e}")

    def _close_stale_sessions(self) -> None:
        """
        F2 — WS market_resolved eventi kaçırılan session'lar için fallback
        kapanış. end_date'i STALE_CLOSE_GRACE saniyeden fazla geçmiş ve hâlâ
        açık olan session'ları, gerçek close_price alınabiliyorsa zorla kapatır.

        Idempotency: _close_session pop-under-lock olduğundan WS kapanış yolu
        ile yarış güvenlidir — yalnızca tek yol callback'leri tetikler, diğeri
        NOOP olur. winning_asset_id WS eventinden gelmediği için outcome
        close_price/ptb'den türetilir (backtest.iter_sessions ile aynı mantık);
        bu yüzden gerçek close_price şarttır — alınamazsa session bu turda
        kapatılmaz, sonraki poll'da tekrar denenir (yanlış outcome yazma riski
        yok). Nadir edge case: Polymarket fiyat API'si o slug için kalıcı
        bozuksa session kapanmaz — WS yolu da aynı API'ye bağımlı.
        """
        now = datetime.now(timezone.utc)
        with self._lock:
            sessions = list(self._sessions.values())

        for session in sessions:
            try:
                end_dt = datetime.fromisoformat(
                    session.end_date.replace("Z", "+00:00")
                )
            except Exception:
                continue

            if (now - end_dt).total_seconds() < STALE_CLOSE_GRACE:
                continue

            close_price = self._fetch_close_price(session)
            if close_price is None:
                logger.debug(
                    f"[REGISTRY] Stale session, close_price henüz yok — "
                    f"sonraki poll'a bırakıldı: {session.slug}"
                )
                continue

            outcome          = "UP" if close_price > session.ptb else "DOWN"
            winning_asset_id = (
                session.yes_token_id if outcome == "UP" else session.no_token_id
            )
            logger.warning(
                f"[REGISTRY] Stale session zorla kapatılıyor (WS market_resolved "
                f"kaçırıldı): {session.slug} → {outcome} | close:{close_price:.4f}"
            )
            self._close_session(
                slug             = session.slug,
                outcome          = outcome,
                close_price      = close_price,
                winning_asset_id = winning_asset_id,
            )

    # ─────────────────────────────────────────────────────────
    # WS EVENT HANDLER'LAR
    # ─────────────────────────────────────────────────────────

    def _handle_new_market_event(
        self,
        market:     str,
        slug:       str,
        assets_ids: list,
        timestamp:  float,
    ) -> None:
        """
        WS'den new_market eventi geldi.
        Slug zaten açıksa atla, değilse Gamma'dan detay çekip aç.
        """
        with self._lock:
            already_open = slug in self._sessions

        if already_open:
            return

        # Slug'dan symbol ve interval çıkar: "btc-updown-5m-1773100500"
        parts = slug.split("-")
        # parts: ["btc", "updown", "5m", "1773100500"]
        if len(parts) < 4:
            return

        symbol   = parts[0].upper()
        interval_str = parts[2].replace("m", "")
        try:
            interval = int(interval_str)
        except ValueError:
            return

        if symbol not in self._symbols or interval not in self._intervals:
            return

        # F1 — Gamma detay çekme + session açma I/O içerir: _find_market_by_slug
        # (HTTP) + _open_session (sleep(5) + PTB retry HTTP), toplam ~50 sn'ye
        # kadar. Bu metod WS dispatch thread'inde çalışıyor; inline çağrı tüm
        # marketlerde snapshot işleme ve sinyal değerlendirmeyi bu süre boyunca
        # dondurur. Kısa ömürlü bir daemon thread'e alarak dispatch thread'i
        # serbest bırak — yalnızca market-açılış eventinde thread yaratılır.
        threading.Thread(
            target=self._open_session_from_slug,
            args=(slug, symbol, interval),
            daemon=True,
        ).start()

    def _open_session_from_slug(
        self, slug: str, symbol: str, interval: int
    ) -> None:
        """
        F1 yardımcı: WS new_market eventinden gelen slug için Gamma detayını
        çekip session'ı açar. Kısa ömürlü daemon thread'de çalışır — WS dispatch
        thread'ini bloklamaz. Hata olursa loglanır ki thread sessizce ölmesin
        (inline çağrıdaki data_bus try/except sarmalaması artık kapsamıyor).
        """
        try:
            market_info = self._find_market_by_slug(slug, symbol, interval)
            if market_info:
                self._open_session(market_info)
        except Exception as e:
            logger.error(f"[REGISTRY] new_market session açma hatası ({slug}): {e}")

    def _handle_market_resolved_event(
        self,
        market:           str,
        assets_ids:       list,
        winning_asset_id: str,
        winning_outcome:  str,
        timestamp:        float,
    ) -> None:
        """
        WS'den market_resolved eventi geldi — kapanış fiyatını çek, session'ı kapat.
        """
        # market_condition_id'den slug bul
        with self._lock:
            slug = None
            for s, session in self._sessions.items():
                if session.market_condition_id == market:
                    slug = s
                    break

        if not slug:
            return

        with self._lock:
            session = self._sessions.get(slug)

        if not session:
            return

        # Determine outcome by comparing winning token ID to the YES token.
        # winning_outcome is an opaque string from the Polymarket WS whose format
        # is not guaranteed — token ID comparison is authoritative.
        outcome = "UP" if winning_asset_id == session.yes_token_id else "DOWN"

        # Polymarket API'den close_price çek
        close_price = self._fetch_close_price(session)

        if close_price is None:
            # Fallback: Binance anlık fiyat
            binance_snap = self._bus.binance.get(session.symbol)
            close_price  = binance_snap["price"] if binance_snap else 0.0
            logger.warning(
                f"[REGISTRY] Close price alınamadı, Binance fallback: "
                f"{session.symbol} {session.interval}dk"
            )

        self._close_session(
            slug             = slug,
            outcome          = outcome,
            close_price      = close_price,
            winning_asset_id = winning_asset_id,
        )

    # ─────────────────────────────────────────────────────────
    # API ÇAĞRILARI
    # ─────────────────────────────────────────────────────────

    def _find_market(self, symbol: str, interval: int) -> Optional[dict]:
        """Mevcut periyodun market bilgisini Gamma'dan çek."""
        now          = int(time.time())
        interval_sec = interval * 60

        for offset in [0, -1, 1]:
            ts   = ((now // interval_sec) + offset) * interval_sec
            slug = f"{symbol.lower()}-updown-{interval}m-{ts}"
            info = self._find_market_by_slug(slug, symbol, interval)
            if info:
                return info
        return None

    def _find_market_by_slug(
        self, slug: str, symbol: str, interval: int
    ) -> Optional[dict]:
        try:
            r = requests.get(f"{GAMMA_BASE}/events/slug/{slug}", timeout=10)
            if r.status_code != 200:
                return None

            data = r.json()
            if not data:
                return None
            if isinstance(data, list):
                data = data[0]
            if not isinstance(data, dict):
                return None

            markets    = data.get("markets") or []
            market     = markets[0] if markets else {}
            raw_tokens = market.get("clobTokenIds", "[]")
            tokens     = json.loads(raw_tokens) if isinstance(raw_tokens, str) else raw_tokens
            if len(tokens) < 2:
                return None

            end_date_str = market.get("endDate") or data.get("endDate", "")
            sr           = 0
            if end_date_str:
                end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                sr     = max(0, int((end_dt - datetime.now(timezone.utc)).total_seconds()))

            # sr > 30sn — erken kapanma bug'ından korunma
            if sr <= 0:
                return None

            return {
                "symbol":               symbol,
                "interval":             interval,
                "slug":                 slug,
                "market_condition_id":  market.get("conditionId", ""),
                "end_date":             end_date_str,
                "yes_token_id":         str(tokens[0]),
                "no_token_id":          str(tokens[1]),
                "seconds_remaining":    sr,
            }
        except Exception as e:
            logger.warning(f"[REGISTRY] find_market_by_slug hatası ({slug}): {e}")
            return None

    def _fetch_ptb(self, symbol: str, market_info: dict) -> Optional[float]:
        """Price To Beat — Polymarket API'den periyot açılış fiyatı."""
        try:
            interval = market_info["interval"]
            end_date = market_info["end_date"]
            end_dt   = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            start_dt = end_dt - timedelta(minutes=interval)
            start_str = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

            variant = VARIANT_MAP.get(interval, "fiveminute")
            url     = (
                f"{POLY_PRICE_BASE}"
                f"?symbol={symbol}"
                f"&eventStartTime={start_str}"
                f"&variant={variant}"
                f"&endDate={end_date}"
            )
            r = requests.get(url, timeout=10)
            if r.status_code != 200:
                return None

            data = r.json()
            return float(data["openPrice"]) if data.get("openPrice") else None
        except Exception as e:
            logger.warning(f"[REGISTRY] PTB alınamadı: {e}")
            return None

    def _fetch_close_price(self, session: MarketSession) -> Optional[float]:
        """
        Periyot kapandıktan sonra close price çek.
        completed=True olana kadar None döner.
        """
        try:
            interval  = session.interval
            end_date  = session.end_date
            end_dt    = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            start_dt  = end_dt - timedelta(minutes=interval)
            start_str = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

            variant = VARIANT_MAP.get(interval, "fiveminute")
            url     = (
                f"{POLY_PRICE_BASE}"
                f"?symbol={session.symbol}"
                f"&eventStartTime={start_str}"
                f"&variant={variant}"
                f"&endDate={end_date}"
            )
            r = requests.get(url, timeout=10)
            if r.status_code != 200:
                return None

            data      = r.json()
            completed = data.get("completed", False)
            if not completed or data.get("closePrice") is None:
                return None

            return float(data["closePrice"])
        except Exception as e:
            logger.warning(f"[REGISTRY] close_price alınamadı: {e}")
            return None
