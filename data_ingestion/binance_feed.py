"""
binance_feed.py — FAZ 1.2
Binance spot fiyat, volatilite ve momentum feed'i.

Her sembol için periyodik olarak güncellenen bir cache tutar.
data_bus.py bu modülden okur — doğrudan HTTP çağrısı yapmaz.

Kullanım:
    feed = BinanceFeed(symbols=["BTC", "ETH", "SOL", "XRP"])
    feed.start()   # arka planda polling başlar (daemon thread)

    snap = feed.get("BTC")
    # {
    #   "price":       83420.5,
    #   "volatility":  0.082,    # % std sapma, son 20 mum
    #   "momentum_3m": 0.014,    # son 3dk % hareket
    #   "momentum_5m": -0.031,   # son 5dk % hareket
    #   "updated_at":  1773100536.5
    # }
"""

import time
import threading
import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)

BINANCE_BASE = "https://api.binance.com/api/v3"

BINANCE_SYMBOLS: dict[str, str] = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
    "XRP": "XRPUSDT",
 "DOGE": "DOGEUSDT",
    "BNB":  "BNBUSDT",

}

# Kaç dakikalık mum kullanılacak
VOLATILITY_WINDOW = 20   # volatilite için
MOMENTUM_3M_WINDOW = 3   # kısa momentum
MOMENTUM_5M_WINDOW = 5   # orta momentum

# Polling aralığı — Binance rate limit: 1200 istek/dakika
# 4 sembol × 3 endpoint = 12 istek/tur
# 5sn'de bir = 144 istek/dakika → güvenli
POLL_INTERVAL = 5


class BinanceFeed:
    """
    Binance spot verilerini arka planda polling ile günceller.
    Thread-safe cache üzerinden okuma sağlar.
    """

    def __init__(self, symbols: Optional[list[str]] = None):
        self._symbols = [s.upper() for s in (symbols or list(BINANCE_SYMBOLS.keys()))]
        self._cache:  dict[str, dict] = {}   # symbol → snapshot
        self._lock    = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

    # ─────────────────────────────────────────────────────────
    # PUBLIC API
    # ─────────────────────────────────────────────────────────

    def start(self) -> None:
        """Polling'i arka planda başlat (daemon thread)."""
        if self._running:
            return
        self._running = True

        # İlk güncellemeyi hemen yap — start() sonrası get() boş dönmesin
        self._update_all()

        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        logger.info(f"[BINANCE] Feed başladı: {self._symbols}")

    def stop(self) -> None:
        self._running = False

    def get(self, symbol: str) -> Optional[dict]:
        """
        Sembol için en güncel snapshot'ı döndür.
        Feed henüz güncellenmemişse None döner.
        """
        with self._lock:
            return self._cache.get(symbol.upper())

    def get_price(self, symbol: str) -> Optional[float]:
        snap = self.get(symbol)
        return snap["price"] if snap else None

    def get_volatility(self, symbol: str) -> Optional[float]:
        snap = self.get(symbol)
        return snap["volatility"] if snap else None

    def is_low_volatility(self, symbol: str, threshold: float = 0.03) -> bool:
        """
        Volatilite düşük mü? (manipülasyon riski yüksek bölge)
        threshold: % cinsinden, default 0.03 = %0.03
        """
        vol = self.get_volatility(symbol)
        if vol is None:
            return False
        return vol < threshold

    # ─────────────────────────────────────────────────────────
    # POLLING DÖNGÜSÜ
    # ─────────────────────────────────────────────────────────

    def _poll_loop(self) -> None:
        while self._running:
            try:
                self._update_all()
            except Exception as e:
                logger.error(f"[BINANCE] Poll hatası: {e}")
            time.sleep(POLL_INTERVAL)

    def _update_all(self) -> None:
        for symbol in self._symbols:
            try:
                snap = self._fetch_snapshot(symbol)
                if snap:
                    with self._lock:
                        self._cache[symbol] = snap
            except Exception as e:
                logger.warning(f"[BINANCE] {symbol} güncellenemedi: {e}")

    # ─────────────────────────────────────────────────────────
    # VERİ ÇEKME
    # ─────────────────────────────────────────────────────────

    def _fetch_snapshot(self, symbol: str) -> Optional[dict]:
        """Sembol için anlık fiyat + volatilite + momentum çek."""
        binance_sym = BINANCE_SYMBOLS.get(symbol)
        if not binance_sym:
            logger.warning(f"[BINANCE] Bilinmeyen sembol: {symbol}")
            return None

        price      = self._fetch_price(binance_sym)
        klines     = self._fetch_klines(binance_sym, limit=max(
            VOLATILITY_WINDOW, MOMENTUM_5M_WINDOW
        ))

        if price is None or not klines:
            return None

        closes     = [float(k[4]) for k in klines]
        volatility = self._calc_volatility(closes[-VOLATILITY_WINDOW:])
        mom_3m     = self._calc_momentum(closes[-MOMENTUM_3M_WINDOW:])
        mom_5m     = self._calc_momentum(closes[-MOMENTUM_5M_WINDOW:])

        return {
            "price":       price,
            "volatility":  volatility,   # % std sapma
            "momentum_3m": mom_3m,       # son 3dk % hareket
            "momentum_5m": mom_5m,       # son 5dk % hareket
            "updated_at":  time.time(),
        }

    def _fetch_price(self, binance_sym: str) -> Optional[float]:
        try:
            r = requests.get(
                f"{BINANCE_BASE}/ticker/price",
                params={"symbol": binance_sym},
                timeout=5,
            )
            r.raise_for_status()
            return float(r.json()["price"])
        except Exception as e:
            logger.warning(f"[BINANCE] Fiyat alınamadı {binance_sym}: {e}")
            return None

    def _fetch_klines(self, binance_sym: str, limit: int) -> Optional[list]:
        try:
            r = requests.get(
                f"{BINANCE_BASE}/klines",
                params={
                    "symbol":   binance_sym,
                    "interval": "1m",
                    "limit":    limit,
                },
                timeout=5,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.warning(f"[BINANCE] Klines alınamadı {binance_sym}: {e}")
            return None

    # ─────────────────────────────────────────────────────────
    # HESAPLAMALAR
    # ─────────────────────────────────────────────────────────

    @staticmethod
    def _calc_volatility(closes: list[float]) -> Optional[float]:
        """
        Kapanış fiyatlarının yüzde standart sapması.
        Düşük  < 0.03%  → manipülasyon riski, bot durmalı
        Normal   0.03–0.15%
        Yüksek > 0.15%  → trend güçlü, sinyal güvenilir
        """
        if len(closes) < 2:
            return None
        mean = sum(closes) / len(closes)
        if mean == 0:
            return None
        std = (sum((x - mean) ** 2 for x in closes) / len(closes)) ** 0.5
        return round(std / mean * 100, 6)

    @staticmethod
    def _calc_momentum(closes: list[float]) -> Optional[float]:
        """
        (son - ilk) / ilk × 100
        Pozitif → yukarı hareket
        Negatif → aşağı hareket
        """
        if len(closes) < 2 or closes[0] == 0:
            return None
        return round((closes[-1] - closes[0]) / closes[0] * 100, 6)
