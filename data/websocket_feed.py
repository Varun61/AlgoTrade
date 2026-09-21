"""
data/websocket_feed.py

Live market data feed via SmartWebSocketV2.

Design rules (do not violate):
  1. WebSocket thread ONLY puts ticks into a queue — no strategy logic here
  2. CandleAggregator runs on the consumer side, NOT in the WS callback
  3. On disconnect, auto-reconnect with exponential backoff

Subscribed tick modes:
  - QUOTE (mode=2): LTP + best bid/ask
  - SNAP_QUOTE (mode=3): Full OHLCV tick (use this for candle building)
"""

from __future__ import annotations
import logging, time, threading
from collections import defaultdict
from datetime import datetime
from queue import Queue, Empty

import pandas as pd

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Candle Aggregator
# ------------------------------------------------------------------

class CandleAggregator:
    """
    Aggregates real-time ticks into N-minute OHLCV candles.

    Emits a completed candle dict when a new candle period starts.
    Thread-safe for single-consumer use.
    """

    def __init__(self, interval_minutes: int) -> None:
        self.interval   = interval_minutes
        self._candles   = {}          # token -> current open candle dict
        self._completed = Queue()     # completed candles go here

    def process_tick(self, token: str, ltp: float, timestamp: datetime,
                     volume: int = 0) -> None:
        """Feed a raw tick. Emits a completed candle if the period rolls over."""
        period = self._floor_timestamp(timestamp)
        candle = self._candles.get(token)

        if candle is None or candle["timestamp"] != period:
            # Close previous candle
            if candle is not None:
                self._completed.put(candle.copy())
            # Open new candle
            self._candles[token] = {
                "token"    : token,
                "timestamp": period,
                "open"     : ltp,
                "high"     : ltp,
                "low"      : ltp,
                "close"    : ltp,
                "volume"   : volume,
            }
        else:
            # Update running candle
            candle["high"]   = max(candle["high"], ltp)
            candle["low"]    = min(candle["low"],  ltp)
            candle["close"]  = ltp
            candle["volume"] += volume

    def get_completed(self, timeout: float = 0.1) -> dict | None:
        """Non-blocking poll for a completed candle. Returns None if empty."""
        try:
            return self._completed.get(timeout=timeout)
        except Empty:
            return None

    def _floor_timestamp(self, ts: datetime) -> datetime:
        """Floor timestamp to the nearest interval boundary."""
        floored_minute = (ts.minute // self.interval) * self.interval
        return ts.replace(minute=floored_minute, second=0, microsecond=0)


# ------------------------------------------------------------------
# WebSocket Feed Handler
# ------------------------------------------------------------------

class LiveFeed:
    """
    Wraps SmartWebSocketV2 to provide a clean tick queue interface.

    Usage:
        feed = LiveFeed(tokens, api_key, client_id, feed_token, jwt_token)
        feed.start()
        while True:
            tick = feed.get_tick()
            if tick: process(tick)
    """

    # SmartWebSocketV2 subscription modes
    LTP        = 1
    QUOTE      = 2
    SNAP_QUOTE = 3

    def __init__(
        self,
        tokens: list[dict],       # [{"exchangeType": 1, "tokens": ["3045", ...]}]
        api_key: str,
        client_id: str,
        feed_token: str,
        jwt_token: str,
        mode: int = 3,            # SNAP_QUOTE for candle building
        reconnect_delay: float = 5.0,
    ) -> None:
        self._tokens         = tokens
        self._api_key        = api_key
        self._client_id      = client_id
        self._feed_token     = feed_token
        self._jwt_token      = jwt_token
        self._mode           = mode
        self._reconnect_delay = reconnect_delay

        self._tick_queue     = Queue(maxsize=10_000)
        self._running        = False
        self._sws            = None
        self._thread         = None

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start WebSocket in a background thread."""
        self._running = True
        self._thread  = threading.Thread(target=self._run_loop, daemon=True, name="LiveFeed")
        self._thread.start()
        logger.info("[LiveFeed] Started background feed thread.")

    def stop(self) -> None:
        """Gracefully shut down the WebSocket."""
        self._running = False
        if self._sws:
            try:
                self._sws.close_connection()
            except Exception:
                pass
        logger.info("[LiveFeed] Stopped.")

    def get_tick(self, timeout: float = 0.1) -> dict | None:
        """Non-blocking poll for the next tick. Returns None if queue is empty."""
        try:
            return self._tick_queue.get(timeout=timeout)
        except Empty:
            return None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        """Main loop: connect, run, reconnect on failure."""
        while self._running:
            try:
                self._connect()
            except Exception as exc:
                logger.error(f"[LiveFeed] WebSocket error: {exc}")

            if self._running:
                logger.warning(f"[LiveFeed] Reconnecting in {self._reconnect_delay}s ...")
                time.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 1.5, 60)

    def _connect(self) -> None:
        from SmartApi.smartWebSocketV2 import SmartWebSocketV2  # type: ignore

        # Installed smartapi-python 1.3.4 still uses ws://, which Angel decommissioned.
        # Force the current secure endpoint regardless of SDK version.
        SmartWebSocketV2.ROOT_URI = "wss://smartapisocket.angelone.in/smart-stream"

        # Monkey-patch SmartWebSocketV2._on_close to support newer websocket-client
        def patched_on_close(self, wsapp, *args, **kwargs):
            if hasattr(self, 'on_close') and callable(self.on_close):
                try:
                    self.on_close(wsapp, *args, **kwargs)
                except TypeError:
                    self.on_close(wsapp)
        SmartWebSocketV2._on_close = patched_on_close

        logger.info(f"[LiveFeed] Connecting to {SmartWebSocketV2.ROOT_URI}")
        self._sws = SmartWebSocketV2(
            auth_token  = self._jwt_token,
            api_key     = self._api_key,
            client_code = self._client_id,
            feed_token  = self._feed_token,
            max_retry_attempt = 0,  # LiveFeed owns reconnect; avoid nested SDK reconnects
        )

        def on_open(wsapp):
            logger.info("[LiveFeed] WebSocket connected. Subscribing ...")
            self._sws.subscribe("live_feed", self._mode, self._tokens)
            self._reconnect_delay = 5.0   # Reset backoff on successful connect

        def on_data(wsapp, message):
            """ONLY enqueue — no strategy logic here."""
            try:
                if self._tick_queue.full():
                    logger.warning("[LiveFeed] Tick queue full, dropping oldest tick.")
                    self._tick_queue.get_nowait()
                self._tick_queue.put_nowait(message)
            except Exception as exc:
                logger.error(f"[LiveFeed] Queue error: {exc}")

        def on_error(wsapp, error):
            logger.error(f"[LiveFeed] WebSocket error: {error}")

        def on_close(wsapp, close_status_code, close_msg):
            logger.warning(f"[LiveFeed] WebSocket closed: {close_status_code} {close_msg}")

        self._sws.on_open  = on_open
        self._sws.on_data  = on_data
        self._sws.on_error = on_error
        self._sws.on_close = on_close
        self._sws.connect()


# ------------------------------------------------------------------
# Tick parser (SmartWebSocketV2 snap_quote format)
# ------------------------------------------------------------------

def parse_snap_quote_tick(raw: dict) -> dict | None:
    """
    Parse a SNAP_QUOTE mode tick from SmartWebSocketV2.
    Returns a normalized dict or None if the tick is malformed.

    SmartAPI snap_quote keys (may vary by SDK version — check logs if parsing fails):
        token, last_traded_price, open_price, high_price, low_price,
        close_price, volume_trade_for_the_day, last_traded_timestamp
    """
    try:
        return {
            "token"    : str(raw.get("token", "")),
            "ltp"      : float(raw.get("last_traded_price", 0)) / 100,   # SDK returns paise
            "open"     : float(raw.get("open_price_of_the_day", 0)) / 100,
            "high"     : float(raw.get("high_price_of_the_day", 0)) / 100,
            "low"      : float(raw.get("low_price_of_the_day", 0)) / 100,
            "close"    : float(raw.get("closed_price", 0)) / 100,
            "volume"   : int(raw.get("volume_trade_for_the_day", 0)),
            "timestamp": datetime.fromtimestamp(
                int(raw.get("exchange_timestamp", time.time() * 1000)) / 1000
            ),
        }
    except Exception as exc:
        logger.debug(f"[parse_tick] Parse error: {exc} | raw={raw}")
        return None
