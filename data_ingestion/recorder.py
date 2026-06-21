"""
recorder.py — FAZ 3.1
DataBus snapshot'larını ve market lifecycle eventlerini JSONL'e kaydeder.

Log formatı (mevcut market_data formatıyla uyumlu):
    {
        "slug":               "btc-updown-5m-1773100500",
        "symbol":             "BTC",
        "interval":           5,
        "recorded_at":        1773100536.5,
        "ptb":                83400.0,
        "open_binance_price": 83420.5,
        "open_volatility":    0.082,
        "open_momentum_5m":   -0.031,
        "close_price":        83450.0,
        "close_volatility":   0.091,
        "actual_outcome":     "UP",
        "snapshots":          [...],
        "market_info":        {...}
    }

Kullanım:
    python recorder.py --symbols BTC ETH SOL XRP --intervals 5 15
"""

import os
import sys
import json
import time
import atexit
import ctypes
import threading
import logging
import argparse
from datetime import datetime
from typing import Optional

from data_bus import DataBus
from market_registry import MarketRegistry, MarketSession

logger = logging.getLogger(__name__)

DEFAULT_LOG_DIR = "logs/market_data"

# Single-instance lock — prevents two recorder processes from appending to the
# same logs/market_data/*.jsonl files (which produces duplicate session records
# and interleaved/malformed lines).
LOCK_PATH = os.path.join("logs", ".recorder.lock")


def _pid_alive(pid: int) -> bool:
    """True if a process with this PID is currently running."""
    if pid <= 0:
        return False
    if os.name == "nt":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(handle)
    else:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True


def _release_lock() -> None:
    """atexit hook — remove the lock file only if this process still owns it."""
    try:
        with open(LOCK_PATH, encoding="utf-8") as f:
            owner = int((f.readline().strip() or "0"))
        if owner == os.getpid():
            os.remove(LOCK_PATH)
    except (OSError, ValueError):
        pass


def _acquire_single_instance_lock() -> None:
    """Acquire the recorder single-instance lock. Exit cleanly if another holds it."""
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)

    if os.path.exists(LOCK_PATH):
        other_pid = 0
        try:
            with open(LOCK_PATH, encoding="utf-8") as f:
                other_pid = int((f.readline().strip() or "0"))
        except (OSError, ValueError):
            other_pid = 0

        if other_pid and other_pid != os.getpid() and _pid_alive(other_pid):
            msg = (
                f"Another recorder is already running (pid={other_pid}). "
                f"Refusing to start a second instance — concurrent recorders "
                f"corrupt logs/market_data with duplicate and interleaved writes."
            )
            logger.critical(f"[RECORDER] {msg}")
            print(f"\nERROR: {msg}\n", file=sys.stderr)
            sys.exit(1)

        logger.warning(
            f"[RECORDER] Stale lock bulundu (pid={other_pid} ölü) — devralınıyor."
        )

    pid = os.getpid()
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(LOCK_PATH, "w", encoding="utf-8") as f:
        f.write(f"{pid}\n{started}\n")
    atexit.register(_release_lock)
    logger.info(f"[RECORDER] Recorder lock acquired pid={pid}")


class Recorder:
    """
    DataBus'a abone olur, her session için snapshot'ları biriktirir,
    session kapanınca JSONL'e yazar.
    """

    def __init__(
        self,
        bus:      DataBus,
        registry: MarketRegistry,
        log_dir:  str = DEFAULT_LOG_DIR,
    ):
        self._bus      = bus
        self._registry = registry
        self._log_dir  = log_dir

        # slug → buffer dict
        self._buffers: dict[str, dict] = {}
        self._lock = threading.Lock()

        # Lifecycle hook'larını registry'ye bağla
        self._registry.set_session_callbacks(
            on_session_opened = self._on_session_opened,
            on_session_closed = self._on_session_closed,
        )

        # DataBus snapshot callback'i
        self._bus.subscribe(self._on_snapshot)

    # ─────────────────────────────────────────────────────────
    # PUBLIC API
    # ─────────────────────────────────────────────────────────

    def start(self) -> None:
        _acquire_single_instance_lock()
        os.makedirs(self._log_dir, exist_ok=True)
        logger.info(f"[RECORDER] Başladı → {self._log_dir}")

    # ─────────────────────────────────────────────────────────
    # SESSION LIFECYCLE
    # ─────────────────────────────────────────────────────────

    def _on_session_opened(self, session: MarketSession) -> None:
        """Yeni periyot açıldı — buffer oluştur."""
        binance_snap = self._bus.binance.get(session.symbol)

        buffer = {
            "session":          session,
            "snapshots":        [],
            "open_binance":     binance_snap["price"]        if binance_snap else None,
            "open_volatility":  binance_snap["volatility"]   if binance_snap else None,
            "open_momentum_5m": binance_snap["momentum_5m"]  if binance_snap else None,
        }

        with self._lock:
            self._buffers[session.slug] = buffer

        logger.info(
            f"[RECORDER] Buffer açıldı: {session.symbol} {session.interval}dk "
            f"| PTB:{session.ptb:.2f}"
        )

    def _on_session_closed(
        self,
        session:          MarketSession,
        outcome:          str,
        close_price:      float,
        winning_asset_id: str,
    ) -> None:
        """Periyot kapandı — JSONL'e yaz."""
        with self._lock:
            buffer = self._buffers.pop(session.slug, None)

        if not buffer:
            logger.warning(f"[RECORDER] Buffer bulunamadı: {session.slug}")
            return

        binance_snap = self._bus.binance.get(session.symbol)

        record = {
            "slug":               session.slug,
            "symbol":             session.symbol,
            "interval":           session.interval,
            "recorded_at":        session.opened_at,
            "ptb":                session.ptb,
            "open_binance_price": buffer["open_binance"],
            "open_volatility":    buffer["open_volatility"],
            "open_momentum_5m":   buffer["open_momentum_5m"],
            "close_price":        close_price,
            "close_volatility":   binance_snap["volatility"] if binance_snap else None,
            "actual_outcome":     outcome,
            "snapshots":          buffer["snapshots"],
            "market_info": {
                "symbol":              session.symbol,
                "interval":            session.interval,
                "slug":                session.slug,
                "market_condition_id": session.market_condition_id,
                "end_date":            session.end_date,
                "yes_token_id":        session.yes_token_id,
                "no_token_id":         session.no_token_id,
            },
        }

        self._save(record)

        logger.info(
            f"[RECORDER] Kaydedildi: {session.symbol} {session.interval}dk "
            f"| PTB:{session.ptb:.2f} → close:{close_price:.2f} → {outcome} "
            f"| {len(buffer['snapshots'])} snapshot"
        )

    # ─────────────────────────────────────────────────────────
    # SNAPSHOT TOPLAMA
    # ─────────────────────────────────────────────────────────

    def _on_snapshot(self, snapshot: dict) -> None:
        """DataBus'tan gelen unified snapshot'ı buffer'a ekle."""
        token_id = snapshot.get("token_id")
        if not token_id:
            return

        session = self._registry.get_session_by_token(token_id)
        if not session:
            return

        snap_record = {
            "t":              snapshot["seconds_remaining"],
            "mid":            snapshot["mid"],
            "spread":         snapshot["spread"],
            "bid_volume":     snapshot["bid_volume"],
            "ask_volume":     snapshot["ask_volume"],
            "imbalance":      snapshot["imbalance"],
            "last_trade":     snapshot.get("last_trade"),
            "binance_price":  snapshot["binance_price"],
            "price_diff_pct": snapshot.get("price_diff_pct"),
            "momentum_3m":    snapshot.get("momentum_3m"),
            "momentum_5m":    snapshot.get("momentum_5m"),
            "timestamp":      snapshot["timestamp"],
        }

        with self._lock:
            buffer = self._buffers.get(session.slug)
            if not buffer:
                return

            # Throttle: aynı saniyede birden fazla snapshot yazma.
            # Signal engine anlık veriyi data_bus'tan okuyor,
            # JSONL sadece backtest için — 1sn granülarite yeterli.
            last = buffer["snapshots"][-1] if buffer["snapshots"] else None
            if last and (snap_record["timestamp"] - last["timestamp"]) < 1.0:
                return

            buffer["snapshots"].append(snap_record)

    # ─────────────────────────────────────────────────────────
    # KAYIT
    # ─────────────────────────────────────────────────────────

    def _save(self, record: dict) -> None:
        try:
            os.makedirs(self._log_dir, exist_ok=True)
            date_str = datetime.utcfromtimestamp(record["recorded_at"]).strftime("%Y-%m-%d")
            filename = f"{record['symbol'].lower()}_{date_str}.jsonl"
            path     = os.path.join(self._log_dir, filename)

            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
        except Exception as e:
            logger.error(f"[RECORDER] Kayıt hatası: {e}")


# ─────────────────────────────────────────────────────────────
# ENTRYPOINT
# ─────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(
        level   = logging.INFO,
        format  = "%(asctime)s [%(levelname)s] %(message)s",
        datefmt = "%H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="Polymarket Market Data Recorder")
    parser.add_argument("--symbols",   nargs="+",           default=["BTC","ETH","SOL","XRP","DOGE","BNB"])
    parser.add_argument("--intervals", nargs="+", type=int, default=[5, 15])
    parser.add_argument("--log-dir",   default=DEFAULT_LOG_DIR)
    args = parser.parse_args()

    bus      = DataBus(symbols=args.symbols)
    registry = MarketRegistry(bus, symbols=args.symbols, intervals=args.intervals)
    recorder = Recorder(bus, registry, log_dir=args.log_dir)

    registry.start()
    recorder.start()

    # DataBus'u ayrı thread'de başlat — blocking
    t = threading.Thread(target=bus.start, daemon=False)
    t.start()

    logger.info("Recorder çalışıyor. Durdurmak için Ctrl+C.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Durduruluyor...")
        bus.stop()


if __name__ == "__main__":
    main()
