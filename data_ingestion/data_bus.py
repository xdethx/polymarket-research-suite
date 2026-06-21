"""
data_bus.py — FAZ 1.3
Polymarket WS + Binance feed'i birleştiren merkezi veri katmanı.

Her token için gelen best_bid_ask veya last_trade eventi,
Binance verisiyle birleştirilip unified snapshot üretilir
ve tüm subscriber callback'lerine iletilir.

Kullanım:
    bus = DataBus(symbols=["BTC", "ETH", "SOL", "XRP"])
    bus.subscribe(on_snapshot)   # her yeni snapshot'ta çağrılır
    bus.start()                  # blocking — ayrı thread'de çalıştır

Snapshot formatı:
    {
        "token_id":          "...",
        "symbol":            "BTC",
        "interval":          5,
        "seconds_remaining": 142,
        "mid":               0.615,
        "spread":            0.010,
        "bid_volume":        340.5,
        "ask_volume":        120.2,
        "imbalance":         0.478,
        "last_trade":        0.610,
        "binance_price":     83420.5,
        "price_diff_pct":    0.024,    # PTB'den beri % fark
        "volatility":        0.082,
        "momentum_3m":       0.014,
        "momentum_5m":      -0.031,
        "timestamp":         1773100536.5,
    }
"""

import threading
import logging
import time
from typing import Callable, Optional

from polymarket_ws import PolymarketWS
from binance_feed import BinanceFeed

logger = logging.getLogger(__name__)

# F6 — Binance feed bu kadar saniyeden uzun süredir güncellenmemişse (outage /
# rate-limit) stale fiyatla snapshot üretme; combined/momentum presetleri eski
# veriyle yanlış sinyal almasın.
BINANCE_STALE_SECONDS = 30


class DataBus:
    """
    Polymarket WS eventlerini Binance verisiyle birleştirip
    subscriber'lara unified snapshot olarak dağıtır.

    market_registry.py bu sınıfı kullanarak token_id → market
    eşlemesini ve PTB değerlerini sağlar.
    """

    def __init__(self, symbols: Optional[list[str]] = None):
        self._symbols = [s.upper() for s in (symbols or ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"])]

        # token_id → market bilgisi (market_registry tarafından doldurulur)
        # {token_id: {"symbol": "BTC", "interval": 5, "ptb": 83400.0,
        #             "end_date": "...", "seconds_remaining": 142}}
        self._token_registry: dict[str, dict] = {}
        self._registry_lock   = threading.Lock()

        # token_id → son book durumu (bid_volume, ask_volume, imbalance)
        # WS'den gelen book snapshot ile güncellenir
        self._book_state: dict[str, dict] = {}
        self._book_lock   = threading.Lock()

        # token_id → son last_trade fiyatı
        self._last_trade: dict[str, float] = {}
        self._trade_lock  = threading.Lock()

        # Subscriber listesi
        self._subscribers: list[Callable] = []
        self._sub_lock     = threading.Lock()

        # Alt modüller
        self._binance = BinanceFeed(symbols=self._symbols)
        self._ws      = PolymarketWS(
            on_best_bid_ask    = self._on_best_bid_ask,
            on_last_trade      = self._on_last_trade,
            on_book_snapshot   = self._on_book_snapshot,
            on_new_market      = self._on_new_market,
            on_market_resolved = self._on_market_resolved,
        )

        # new_market / market_resolved için dış callback'ler
        self._on_new_market_cb:      Optional[Callable] = None
        self._on_market_resolved_cb: Optional[Callable] = None

    # ─────────────────────────────────────────────────────────
    # PUBLIC API
    # ─────────────────────────────────────────────────────────

    def subscribe(self, callback: Callable) -> None:
        """
        Unified snapshot callback'i ekle.
        callback(snapshot: dict) — her yeni snapshot'ta çağrılır.
        """
        with self._sub_lock:
            self._subscribers.append(callback)

    def set_lifecycle_callbacks(
        self,
        on_new_market:      Optional[Callable] = None,
        on_market_resolved: Optional[Callable] = None,
    ) -> None:
        """
        Market lifecycle eventleri için callback'leri ayarla.
        market_registry.py tarafından kullanılır.

        on_new_market(market, slug, assets_ids, timestamp)
        on_market_resolved(market, assets_ids, winning_asset_id,
                           winning_outcome, timestamp)
        """
        self._on_new_market_cb      = on_new_market
        self._on_market_resolved_cb = on_market_resolved

    def register_token(
        self,
        token_id:          str,
        symbol:            str,
        interval:          int,
        ptb:               float,
        end_date:          str,
        seconds_remaining: int = 0,
    ) -> None:
        """
        market_registry.py tarafından çağrılır.
        Token'ı registry'ye ekler ve WS'e subscribe eder.
        """
        with self._registry_lock:
            self._token_registry[token_id] = {
                "symbol":            symbol.upper(),
                "interval":          interval,
                "ptb":               ptb,
                "end_date":          end_date,
                "seconds_remaining": seconds_remaining,
            }
        self._ws.subscribe([token_id])
        logger.info(f"[BUS] Token kayıt edildi: {symbol} {interval}dk | {token_id[:8]}...")

    def unregister_token(self, token_id: str) -> None:
        """Kapanan market'in token'ını temizle."""
        with self._registry_lock:
            self._token_registry.pop(token_id, None)
        with self._book_lock:
            self._book_state.pop(token_id, None)
        with self._trade_lock:
            self._last_trade.pop(token_id, None)
        self._ws.unsubscribe([token_id])

    def update_seconds_remaining(self, token_id: str, seconds_remaining: int) -> None:
        """market_registry her döngüde sr'yi günceller."""
        with self._registry_lock:
            if token_id in self._token_registry:
                self._token_registry[token_id]["seconds_remaining"] = seconds_remaining

    def start(self) -> None:
        """
        Binance feed'i ve WS'i başlat — blocking.
        Ayrı thread'de çalıştır:
            t = threading.Thread(target=bus.start, daemon=True)
            t.start()
        """
        # Binance hemen başlasın
        self._binance.start()

        # Reconnect callback: her bağlantıda sadece aktif tokenları subscribe et
        # Böylece eski/expired tokenlar birikmez
        self._ws.set_reconnect_callback(self._get_active_tokens)

        # WS blocking — bu satır run_forever ile döner
        self._ws.start()

    def _get_active_tokens(self) -> list:
        """
        Reconnect callback: o an gerçekten aktif olan token'ları döndür.
        end_date geçmiş stale session'ları temizler — bunlar INVALID OPERATION
        hatasına ve bağlantı kopmalarına neden olur.
        """
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        stale = []

        with self._registry_lock:
            for token_id, meta in list(self._token_registry.items()):
                end_date = meta.get("end_date", "")
                if not end_date:
                    continue
                try:
                    end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                    if end_dt < now:
                        stale.append(token_id)
                except Exception:
                    pass

        # Stale token'ları registry'den temizle
        for token_id in stale:
            with self._registry_lock:
                self._token_registry.pop(token_id, None)
            with self._book_lock:
                self._book_state.pop(token_id, None)
            with self._trade_lock:
                self._last_trade.pop(token_id, None)

        if stale:
            import logging
            logging.getLogger(__name__).info(
                f"[BUS] Reconnect: {len(stale)} stale token temizlendi"
            )

        with self._registry_lock:
            return list(self._token_registry.keys())

    def stop(self) -> None:
        self._binance.stop()
        self._ws.stop()

    @property
    def binance(self) -> BinanceFeed:
        """Doğrudan Binance feed'ine erişim — market_registry için."""
        return self._binance

    @property
    def ws(self) -> PolymarketWS:
        """Doğrudan WS'e erişim — market_registry için."""
        return self._ws

    # ─────────────────────────────────────────────────────────
    # WS EVENT HANDLER'LAR
    # ─────────────────────────────────────────────────────────

    def _on_book_snapshot(
        self, token_id: str, bids: list, asks: list, timestamp: float
    ) -> None:
        """
        Initial dump — bağlanınca gelen tam orderbook.
        bid/ask volume ve imbalance hesaplanır, cache'e yazılır.
        Snapshot üretilmez (best_bid_ask bunu yapacak).
        """
        book = self._parse_book_volumes(bids, asks)
        with self._book_lock:
            self._book_state[token_id] = book

    def _on_best_bid_ask(
        self,
        token_id:  str,
        best_bid:  float,
        best_ask:  float,
        spread:    float,
        timestamp: float,
    ) -> None:
        """
        Her fiyat değişiminde gelir — ana snapshot tetikleyicisi.
        """
        mid = round((best_bid + best_ask) / 2, 4)

        with self._book_lock:
            book = self._book_state.get(token_id, {})

        with self._trade_lock:
            last_trade = self._last_trade.get(token_id)

        snapshot = self._build_snapshot(
            token_id   = token_id,
            mid        = mid,
            spread     = spread,
            book       = book,
            last_trade = last_trade,
            timestamp  = timestamp,
        )

        if snapshot:
            self._emit(snapshot)

    def _on_last_trade(
        self,
        token_id:  str,
        price:     float,
        size:      float,
        side:      str,
        timestamp: float,
    ) -> None:
        """Gerçek işlem fiyatını cache'e yaz."""
        with self._trade_lock:
            self._last_trade[token_id] = price

    def _on_new_market(
        self,
        market:     str,
        slug:       str,
        assets_ids: list,
        timestamp:  float,
    ) -> None:
        """market_registry'ye ilet."""
        if self._on_new_market_cb:
            try:
                self._on_new_market_cb(
                    market     = market,
                    slug       = slug,
                    assets_ids = assets_ids,
                    timestamp  = timestamp,
                )
            except Exception as e:
                logger.error(f"[BUS] new_market callback hatası: {e}")

    def _on_market_resolved(
        self,
        market:           str,
        assets_ids:       list,
        winning_asset_id: str,
        winning_outcome:  str,
        timestamp:        float,
    ) -> None:
        """market_registry'ye ilet."""
        if self._on_market_resolved_cb:
            try:
                self._on_market_resolved_cb(
                    market            = market,
                    assets_ids        = assets_ids,
                    winning_asset_id  = winning_asset_id,
                    winning_outcome   = winning_outcome,
                    timestamp         = timestamp,
                )
            except Exception as e:
                logger.error(f"[BUS] market_resolved callback hatası: {e}")

    # ─────────────────────────────────────────────────────────
    # SNAPSHOT ÜRETME
    # ─────────────────────────────────────────────────────────

    def _build_snapshot(
        self,
        token_id:   str,
        mid:        float,
        spread:     float,
        book:       dict,
        last_trade: Optional[float],
        timestamp:  float,
    ) -> Optional[dict]:
        """
        Polymarket + Binance verilerini birleştir.
        Token registry'de yoksa None döner.
        """
        with self._registry_lock:
            meta = self._token_registry.get(token_id)

        if not meta:
            return None

        symbol   = meta["symbol"]
        interval = meta["interval"]
        ptb      = meta["ptb"]
        sr       = meta["seconds_remaining"]

        binance_snap = self._binance.get(symbol)
        if not binance_snap:
            # Binance henüz güncellenmemişse snapshot bekle
            return None

        # F6 — stale-cache koruması: feed donmuşsa (outage/rate-limit) eski
        # fiyatla snapshot üretme. 'not binance_snap' ile aynı semantik:
        # sessizce None dön — log spam yok (binance_feed zaten fetch hatasını
        # loglar). Snapshot her best_bid_ask'te çağrılır.
        updated_at = binance_snap.get("updated_at", 0)
        if updated_at and (time.time() - updated_at) > BINANCE_STALE_SECONDS:
            return None

        binance_price = binance_snap["price"]
        price_diff_pct = (
            round((binance_price - ptb) / ptb * 100, 6)
            if ptb and ptb > 0
            else None
        )

        return {
            "token_id":          token_id,
            "symbol":            symbol,
            "interval":          interval,
            "seconds_remaining": sr,
            # Polymarket
            "mid":               mid,
            "spread":            spread,
            "bid_volume":        book.get("bid_volume", 0.0),
            "ask_volume":        book.get("ask_volume", 0.0),
            "imbalance":         book.get("imbalance", 0.0),
            "last_trade":        last_trade,
            # Binance
            "binance_price":     binance_price,
            "price_diff_pct":    price_diff_pct,
            "volatility":        binance_snap["volatility"],
            "momentum_3m":       binance_snap["momentum_3m"],
            "momentum_5m":       binance_snap["momentum_5m"],
            "timestamp":         timestamp,
        }

    # ─────────────────────────────────────────────────────────
    # YARDIMCI
    # ─────────────────────────────────────────────────────────

    def _parse_book_volumes(self, bids: list, asks: list) -> dict:
        """Top 5 kademeden bid/ask volume ve imbalance hesapla."""
        try:
            bid_vol = sum(float(b["size"]) for b in bids[:5])
            ask_vol = sum(float(a["size"]) for a in asks[:5])
            total   = bid_vol + ask_vol
            imbalance = round((bid_vol - ask_vol) / total, 4) if total > 0 else 0.0
            return {
                "bid_volume": round(bid_vol, 2),
                "ask_volume": round(ask_vol, 2),
                "imbalance":  imbalance,
            }
        except Exception:
            return {"bid_volume": 0.0, "ask_volume": 0.0, "imbalance": 0.0}

    def _emit(self, snapshot: dict) -> None:
        """Snapshot'ı tüm subscriber'lara ilet."""
        with self._sub_lock:
            subscribers = list(self._subscribers)

        for cb in subscribers:
            try:
                cb(snapshot)
            except Exception as e:
                logger.error(f"[BUS] Subscriber hatası: {e}")
