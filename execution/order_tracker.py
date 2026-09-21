"""
execution/order_tracker.py

Polls the order book to reconcile order states.
Detects fills, rejections, and partial fills.
Notifies the position manager and circuit breaker on fill events.

Polling interval: every 5 seconds (configurable).
Alternative: use WebSocket order updates when available in SmartAPI.
"""

from __future__ import annotations
import logging, time, threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class OrderTracker:
    """
    Polls SmartAPI orderBook() to track order lifecycle.

    Callbacks:
        on_fill(order_id, avg_price, qty)    — called when order is fully filled
        on_reject(order_id, reason)          — called when order is rejected
        on_partial(order_id, filled_qty)     — called on partial fill
    """

    # SmartAPI order status strings
    OPEN_STATUSES     = {"open", "pending", "trigger pending", "after market order req received"}
    FILLED_STATUSES   = {"complete"}
    REJECTED_STATUSES = {"rejected", "cancelled"}

    def __init__(
        self,
        order_manager,
        poll_interval: float = 5.0,
        on_fill   : Optional[Callable] = None,
        on_reject : Optional[Callable] = None,
        on_partial: Optional[Callable] = None,
    ) -> None:
        self._om            = order_manager
        self._poll_interval = poll_interval
        self._on_fill       = on_fill    or (lambda *a: None)
        self._on_reject     = on_reject  or (lambda *a: None)
        self._on_partial    = on_partial or (lambda *a: None)

        self._watching      : dict[str, dict] = {}   # order_id -> metadata
        self._running       = False
        self._thread        : Optional[threading.Thread] = None

    def watch(self, order_id: str, client_ref: str = "") -> None:
        """Register an order_id for tracking."""
        self._watching[order_id] = {"client_ref": client_ref, "last_status": "open"}
        logger.debug(f"[OrderTracker] Watching {order_id}")

    def unwatch(self, order_id: str) -> None:
        self._watching.pop(order_id, None)

    def start(self) -> None:
        self._running = True
        self._thread  = threading.Thread(target=self._poll_loop, daemon=True, name="OrderTracker")
        self._thread.start()
        logger.info("[OrderTracker] Started polling loop.")

    def stop(self) -> None:
        self._running = False
        logger.info("[OrderTracker] Stopped.")

    def _poll_loop(self) -> None:
        while self._running:
            try:
                self._reconcile()
            except Exception as exc:
                logger.error(f"[OrderTracker] Poll error: {exc}")
            time.sleep(self._poll_interval)

    def _reconcile(self) -> None:
        if not self._watching:
            return

        orders = self._om.get_order_book()
        order_map = {o.get("orderid", ""): o for o in orders}

        for order_id in list(self._watching.keys()):
            order = order_map.get(order_id)
            if not order:
                logger.debug(f"[OrderTracker] {order_id} not in order book yet.")
                continue

            status   = str(order.get("status", "")).lower()
            avg_price = float(order.get("averageprice", 0) or 0)
            filled    = int(order.get("filledshares", 0) or 0)
            total_qty = int(order.get("quantity", 0) or 0)
            reject_reason = order.get("text", "")

            meta = self._watching.get(order_id, {})
            last = meta.get("last_status", "")

            if status in self.FILLED_STATUSES and last != "complete":
                logger.info(f"[OrderTracker] ✅ FILLED {order_id} @ ₹{avg_price:.2f} | qty={filled}")
                self._on_fill(order_id, avg_price, filled)
                meta["last_status"] = "complete"
                self.unwatch(order_id)

            elif status in self.REJECTED_STATUSES and last not in self.REJECTED_STATUSES:
                logger.warning(f"[OrderTracker] ❌ REJECTED {order_id}: {reject_reason}")
                self._on_reject(order_id, reject_reason)
                meta["last_status"] = status
                self.unwatch(order_id)

            elif status in self.OPEN_STATUSES and filled > 0 and last != f"partial_{filled}":
                logger.info(f"[OrderTracker] ⚡ PARTIAL {order_id}: {filled}/{total_qty} filled")
                self._on_partial(order_id, filled)
                meta["last_status"] = f"partial_{filled}"
