"""
execution/order_manager.py

Places, modifies, and cancels orders via SmartAPI.
Implements idempotency to prevent duplicate orders on network retries.

Design:
  - Maintains a pending_orders dict: {client_order_ref -> order_id}
  - Before placing, checks if a ref already has an active order
  - Adds exponential backoff retry for transient failures

Mode-awareness:
  - In "paper" mode (settings.yaml trading.mode), orders are simulated locally
  - In "live" mode, real API calls are made
"""

from __future__ import annotations
import logging, time, uuid
from typing import Optional
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_SETTINGS_PATH = Path(__file__).parent.parent / "config" / "settings.yaml"


def _get_mode() -> str:
    with open(_SETTINGS_PATH) as f:
        return yaml.safe_load(f)["trading"]["mode"]


class OrderManager:
    """
    Idempotent order placement with paper/live mode switching.

    Args:
        smart_obj: Authenticated SmartConnect instance (can be None in paper mode)
    """

    def __init__(self, smart_obj=None) -> None:
        self.obj           = smart_obj
        self.mode          = _get_mode()
        self._pending      : dict[str, str] = {}   # ref -> order_id
        self._paper_orders : list[dict]     = []   # Simulated order book
        self._paper_id_ctr : int            = 1000

        logger.info(f"[OrderManager] Mode: {self.mode.upper()}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def place_order(
        self,
        symbol      : str,
        token       : str,
        exchange    : str,
        transaction : str,    # "BUY" | "SELL"
        qty         : int,
        order_type  : str = "MARKET",
        price       : float = 0,
        product     : str = "INTRADAY",
        client_ref  : Optional[str] = None,
        retries     : int = 3,
    ) -> str | None:
        """
        Place an order. Returns order_id or None on failure.

        Args:
            client_ref: Unique reference for idempotency. Auto-generated if None.
        """
        if qty <= 0:
            logger.warning(f"[OrderManager] Zero/negative qty for {symbol} — skipping.")
            return None

        client_ref = client_ref or str(uuid.uuid4())[:16]

        # Idempotency check
        if client_ref in self._pending:
            logger.warning(f"[OrderManager] Duplicate order ref {client_ref} — already placed: "
                           f"{self._pending[client_ref]}")
            return self._pending[client_ref]

        if self.mode == "paper":
            return self._paper_place(symbol, token, exchange, transaction, qty,
                                      order_type, price, product, client_ref)

        # Live order
        params = {
            "variety"        : "NORMAL",
            "tradingsymbol"  : symbol,
            "symboltoken"    : token,
            "transactiontype": transaction.upper(),
            "exchange"       : exchange.upper(),
            "ordertype"      : order_type.upper(),
            "producttype"    : product.upper(),
            "duration"       : "DAY",
            "quantity"       : str(qty),
            "price"          : str(price) if price else "0",
            "squareoff"      : "0",
            "stoploss"       : "0",
        }

        for attempt in range(1, retries + 1):
            try:
                resp = self.obj.placeOrder(params)
                if resp and resp.get("status"):
                    order_id = resp["data"]["orderid"]
                    self._pending[client_ref] = order_id
                    logger.info(f"[OrderManager] ✅ Placed {transaction} {qty}x{symbol} "
                                f"| OrderID={order_id}")
                    return order_id
                logger.warning(f"[OrderManager] Attempt {attempt} failed: {resp}")
            except Exception as exc:
                logger.warning(f"[OrderManager] Exception attempt {attempt}: {exc}")
            time.sleep(2 ** attempt)

        logger.error(f"[OrderManager] ❌ Order failed after {retries} attempts: {symbol}")
        return None

    def cancel_order(self, order_id: str, variety: str = "NORMAL") -> bool:
        """Cancel an order by order_id."""
        if self.mode == "paper":
            self._paper_cancel(order_id)
            return True
        try:
            resp = self.obj.cancelOrder(order_id, variety)
            if resp and resp.get("status"):
                logger.info(f"[OrderManager] Cancelled order {order_id}")
                # Remove from pending
                self._pending = {k: v for k, v in self._pending.items() if v != order_id}
                return True
        except Exception as exc:
            logger.error(f"[OrderManager] Cancel failed: {exc}")
        return False

    def get_order_book(self) -> list[dict]:
        """Fetch full order book."""
        if self.mode == "paper":
            return self._paper_orders.copy()
        try:
            resp = self.obj.orderBook()
            return resp.get("data", []) if resp else []
        except Exception as exc:
            logger.error(f"[OrderManager] orderBook() error: {exc}")
            return []

    def mark_filled(self, client_ref: str) -> None:
        """Remove from pending tracking once confirmed filled."""
        self._pending.pop(client_ref, None)

    # ------------------------------------------------------------------
    # Paper trading helpers
    # ------------------------------------------------------------------

    def _paper_place(self, symbol, token, exchange, transaction,
                     qty, order_type, price, product, client_ref) -> str:
        order_id = f"PAPER-{self._paper_id_ctr:04d}"
        self._paper_id_ctr += 1
        order = {
            "orderid"        : order_id,
            "tradingsymbol"  : symbol,
            "symboltoken"    : token,
            "transactiontype": transaction,
            "quantity"       : qty,
            "ordertype"      : order_type,
            "status"         : "complete",    # Paper: instantly filled
            "client_ref"     : client_ref,
            "avgprice"       : price,
        }
        self._paper_orders.append(order)
        self._pending[client_ref] = order_id
        logger.info(f"[OrderManager] 📝 PAPER {transaction} {qty}x{symbol} | {order_id}")
        return order_id

    def _paper_cancel(self, order_id: str) -> None:
        for o in self._paper_orders:
            if o["orderid"] == order_id:
                o["status"] = "cancelled"
                logger.info(f"[OrderManager] 📝 PAPER cancel {order_id}")
                return
