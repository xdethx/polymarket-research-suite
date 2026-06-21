"""
polymarket_ws.py — FAZ 1.1
Polymarket Market Channel WebSocket istemcisi.

Bağlantı: wss://ws-subscriptions-clob.polymarket.com/ws/market
Desteklenen eventler:
  - book             → initial orderbook snapshot
  - price_change     → orderbook delta
  - best_bid_ask     → anlık mid/spread (custom_feature_enabled=True)
  - last_trade_price → gerçek işlem fiyatı + hacim
  - new_market       → yeni periyot açıldı
  - market_resolved  → periyot kapandı, kazanan belli oldu

Kullanım:
  ws = PolymarketWS(
      on_best_bid_ask    = handle_book,
      on_last_trade      = handle_trade,
      on_new_market      = handle_new_market,
      on_market_resolved = handle_resolved,
  )
  ws.subscribe(["token_id_1", "token_id_2"])
  ws.start()   # blocking — ayrı thread'de çalıştır
"""

import json
import time
import threading
import logging
from typing import Callable, Optional

# websocket-client kütüphanesi gerekli:
# pip install websocket-client
import websocket

logger = logging.getLogger(__name__)

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# Ping aralığı — docs "send every 10 seconds" diyor
PING_INTERVAL  = 10
# Reconnect bekleme süresi (üstel artış için başlangıç)
RECONNECT_BASE = 5
RECONNECT_MAX  = 60


class PolymarketWS:
    """
    Polymarket Market Channel WebSocket istemcisi.

    Callbacks:
        on_best_bid_ask(token_id, best_bid, best_ask, spread, timestamp)
        on_last_trade(token_id, price, size, side, timestamp)
        on_new_market(token_id, market, slug, assets_ids, timestamp)
        on_market_resolved(token_id, winning_asset_id, winning_outcome, timestamp)
        on_book_snapshot(token_id, bids, asks, timestamp)   ← initial dump
    """

    def __init__(
        self,
        on_best_bid_ask:    Optional[Callable] = None,
        on_last_trade:      Optional[Callable] = None,
        on_new_market:      Optional[Callable] = None,
        on_market_resolved: Optional[Callable] = None,
        on_book_snapshot:   Optional[Callable] = None,
    ):
        self._on_best_bid_ask    = on_best_bid_ask
        self._on_last_trade      = on_last_trade
        self._on_new_market      = on_new_market
        self._on_market_resolved = on_market_resolved
        self._on_book_snapshot   = on_book_snapshot
        # Reconnect'te çağrılır — güncel aktif token listesini döndürmeli
        self._on_reconnect: Optional[callable] = None

        self._subscribed_tokens: list[str] = []
        self._ws:          Optional[websocket.WebSocketApp] = None
        self._running:     bool  = False
        self._connected:   bool  = False
        self._reconnect_delay: int = RECONNECT_BASE

        self._ping_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    # ─────────────────────────────────────────────────────────
    # PUBLIC API
    # ─────────────────────────────────────────────────────────

    def subscribe(self, token_ids: list[str]) -> None:
        """
        Token ID'yi anlık olarak subscribe et.
        _subscribed_tokens'a EKLEMEZ — reconnect callback registry'den alır.
        Böylece eski tokenlar birikmez.
        """
        if self._connected:
            self._send_subscribe_update(token_ids)
            logger.info(f"[WS] Yeni tokenlar subscribe edildi: {len(token_ids)} adet")

    def unsubscribe(self, token_ids: list[str]) -> None:
        """Çalışır durumda token listesinden çıkar."""
        with self._lock:
            for tid in token_ids:
                if tid in self._subscribed_tokens:
                    self._subscribed_tokens.remove(tid)

        if self._connected:
            self._send_unsubscribe_update(token_ids)

    def start(self) -> None:
        """
        WebSocket döngüsünü başlat — blocking.
        Ayrı bir thread'de çalıştır:
            t = threading.Thread(target=ws.start, daemon=True)
            t.start()
        """
        self._running = True
        while self._running:
            try:
                self._connect()
            except Exception as e:
                logger.error(f"[WS] Bağlantı hatası: {e}")

            if not self._running:
                break

            logger.info(f"[WS] {self._reconnect_delay}sn sonra yeniden bağlanılıyor...")
            time.sleep(self._reconnect_delay)
            # Üstel backoff — max 60sn
            self._reconnect_delay = min(self._reconnect_delay * 2, RECONNECT_MAX)

    def set_reconnect_callback(self, callback) -> None:
        """
        Reconnect olduğunda çağrılır.
        callback() → list[str] — o an aktif token_id listesini döndürmeli.
        Bu sayede reconnect'te birikmiş eski tokenlar yerine
        sadece gerçekten açık session'lar subscribe edilir.
        """
        self._on_reconnect = callback

    def stop(self) -> None:
        """WebSocket bağlantısını kapat."""
        self._running = False
        if self._ws:
            self._ws.close()

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ─────────────────────────────────────────────────────────
    # BAĞLANTI
    # ─────────────────────────────────────────────────────────

    def _connect(self) -> None:
        self._ws = websocket.WebSocketApp(
            WS_URL,
            on_open    = self._on_open,
            on_message = self._on_message,
            on_error   = self._on_error,
            on_close   = self._on_close,
        )
        self._ws.run_forever()

    def _on_open(self, ws) -> None:
        self._connected      = True
        self._reconnect_delay = RECONNECT_BASE  # başarılı bağlantı → sıfırla
        logger.info("[WS] Bağlantı kuruldu")

        # Reconnect callback varsa güncel aktif token listesini al
        # Bu sayede birikmiş eski/expired tokenlar subscribe edilmez
        if self._on_reconnect:
            try:
                fresh_tokens = self._on_reconnect()
                if fresh_tokens is not None:
                    with self._lock:
                        self._subscribed_tokens = list(fresh_tokens)
            except Exception as e:
                logger.error(f"[WS] Reconnect callback hatası: {e}")

        # Subscription
        with self._lock:
            tokens = list(self._subscribed_tokens)
        if tokens:
            self._send_initial_subscription(tokens)

        # Ping thread başlat
        self._ping_thread = threading.Thread(
            target=self._ping_loop, daemon=True
        )
        self._ping_thread.start()

    def _on_close(self, ws, close_status_code, close_msg) -> None:
        self._connected = False
        logger.info(f"[WS] Bağlantı kapandı: {close_status_code} {close_msg}")

    def _on_error(self, ws, error) -> None:
        logger.error(f"[WS] Hata: {error}")

    # ─────────────────────────────────────────────────────────
    # MESAJ GÖNDERİMİ
    # ─────────────────────────────────────────────────────────

    def _send_initial_subscription(self, token_ids: list[str]) -> None:
        # Polymarket WS token limiti var — büyük liste anında kopuyor.
        # 10'ar batch halinde gönder.
        BATCH_SIZE = 10
        for i in range(0, len(token_ids), BATCH_SIZE):
            batch = token_ids[i:i + BATCH_SIZE]
            msg = {
                "assets_ids":             batch,
                "type":                   "market",
                "initial_dump":           True,
                "level":                  2,
                "custom_feature_enabled": True,
            }
            self._send(msg)
            if i + BATCH_SIZE < len(token_ids):
                time.sleep(0.3)
        logger.info(f"[WS] Subscribe: {len(token_ids)} token")

    def _send_subscribe_update(self, token_ids: list[str]) -> None:
        msg = {
            "operation":             "subscribe",
            "assets_ids":            token_ids,
            "custom_feature_enabled": True,
        }
        self._send(msg)

    def _send_unsubscribe_update(self, token_ids: list[str]) -> None:
        msg = {
            "operation": "unsubscribe",
            "assets_ids": token_ids,
        }
        self._send(msg)

    def _send(self, data: dict) -> None:
        if self._ws and self._connected:
            try:
                self._ws.send(json.dumps(data))
            except Exception as e:
                logger.error(f"[WS] Gönderim hatası: {e}")

    def _ping_loop(self) -> None:
        """Her 10sn 'PING' gönder, bağlantı canlı tut."""
        while self._connected:
            time.sleep(PING_INTERVAL)
            if self._connected and self._ws:
                try:
                    self._ws.send("PING")
                except Exception:
                    break

    # ─────────────────────────────────────────────────────────
    # MESAJ PARSE
    # ─────────────────────────────────────────────────────────

    def _on_message(self, ws, raw: str) -> None:
        # Pong yanıtı
        if raw == "PONG":
            return

        # Polymarket WS hata mesajları (JSON değil)
        if raw in ("INVALID OPERATION", "INVALID_OPERATION"):
            logger.warning("[WS] Geçersiz operasyon — mesaj atlandı")
            return

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # Polymarket bazen düz metin hata mesajı dönebilir
            if len(raw) < 200:
                logger.warning(f"[WS] Parse hatası: {raw}")
            return

        # Tek event veya liste olarak gelebilir
        events = data if isinstance(data, list) else [data]

        for event in events:
            self._dispatch(event)

    def _dispatch(self, event: dict) -> None:
        event_type = event.get("event_type")

        if event_type == "book":
            self._handle_book_snapshot(event)

        elif event_type == "best_bid_ask":
            self._handle_best_bid_ask(event)

        elif event_type == "last_trade_price":
            self._handle_last_trade(event)

        elif event_type == "new_market":
            self._handle_new_market(event)

        elif event_type == "market_resolved":
            self._handle_market_resolved(event)

        elif event_type == "price_change":
            pass  # book delta — şu an kullanmıyoruz, best_bid_ask yeterli

        # event_type yoksa veya bilinmiyorsa sessizce geç

    # ─────────────────────────────────────────────────────────
    # EVENT HANDLER'LAR
    # ─────────────────────────────────────────────────────────

    def _handle_book_snapshot(self, event: dict) -> None:
        if not self._on_book_snapshot:
            return
        try:
            self._on_book_snapshot(
                token_id  = event["asset_id"],
                bids      = event.get("bids", []),
                asks      = event.get("asks", []),
                timestamp = int(event.get("timestamp", 0)) / 1000,
            )
        except Exception as e:
            logger.error(f"[WS] book_snapshot handler hatası: {e}")

    def _handle_best_bid_ask(self, event: dict) -> None:
        if not self._on_best_bid_ask:
            return
        try:
            self._on_best_bid_ask(
                token_id  = event["asset_id"],
                best_bid  = float(event["best_bid"]),
                best_ask  = float(event["best_ask"]),
                spread    = float(event["spread"]),
                timestamp = int(event.get("timestamp", 0)) / 1000,
            )
        except Exception as e:
            logger.error(f"[WS] best_bid_ask handler hatası: {e}")

    def _handle_last_trade(self, event: dict) -> None:
        if not self._on_last_trade:
            return
        try:
            self._on_last_trade(
                token_id  = event["asset_id"],
                price     = float(event["price"]),
                size      = float(event["size"]),
                side      = event.get("side", ""),
                timestamp = int(event.get("timestamp", 0)) / 1000,
            )
        except Exception as e:
            logger.error(f"[WS] last_trade handler hatası: {e}")

    def _handle_new_market(self, event: dict) -> None:
        if not self._on_new_market:
            return
        try:
            self._on_new_market(
                market     = event.get("market", ""),
                slug       = event.get("slug", ""),
                assets_ids = event.get("assets_ids", []),
                timestamp  = int(event.get("timestamp", 0)) / 1000,
            )
        except Exception as e:
            logger.error(f"[WS] new_market handler hatası: {e}")

    def _handle_market_resolved(self, event: dict) -> None:
        if not self._on_market_resolved:
            return
        try:
            self._on_market_resolved(
                market            = event.get("market", ""),
                assets_ids        = event.get("assets_ids", []),
                winning_asset_id  = event.get("winning_asset_id", ""),
                winning_outcome   = event.get("winning_outcome", ""),
                timestamp         = int(event.get("timestamp", 0)) / 1000,
            )
        except Exception as e:
            logger.error(f"[WS] market_resolved handler hatası: {e}")
