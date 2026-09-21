"""
execution/position_manager.py

Tracks all open positions and computes unrealized + realized P&L.
Also manages forced square-off at end-of-day time.

Data model:
    position = {
        "symbol"      : "SBIN-EQ",
        "token"       : "3045",
        "direction"   : "long" | "short",
        "qty"         : 100,
        "entry_price" : 450.0,
        "stop_loss"   : 443.5,
        "target"      : 461.25,
        "entry_order_id" : "12345",
        "exit_order_id"  : None,
        "realized_pnl"   : 0.0,
    }
"""

from __future__ import annotations
import logging

logger = logging.getLogger(__name__)


class PositionManager:
    """
    Maintains the current open positions and realized P&L for the session.
    """

    def __init__(self) -> None:
        self._positions: dict[str, dict] = {}   # token -> position
        self._realized_pnl: float = 0.0
        self._closed_trades: list[dict] = []

    # ------------------------------------------------------------------
    # Position lifecycle
    # ------------------------------------------------------------------

    def open_position(
        self,
        symbol      : str,
        token       : str,
        direction   : str,     # "long" | "short"
        qty         : int,
        entry_price : float,
        stop_loss   : float,
        target      : float,
        order_id    : str,
    ) -> None:
        if token in self._positions:
            logger.warning(f"[PositionMgr] Already have position in {symbol} — skipping open.")
            return

        self._positions[token] = {
            "symbol"         : symbol,
            "token"          : token,
            "direction"      : direction,
            "qty"            : qty,
            "entry_price"    : entry_price,
            "stop_loss"      : stop_loss,
            "target"         : target,
            "entry_order_id" : order_id,
            "exit_order_id"  : None,
            "realized_pnl"   : 0.0,
        }
        logger.info(f"[PositionMgr] Opened {direction.upper()} {qty}x{symbol} @ ₹{entry_price:.2f} "
                    f"| SL={stop_loss:.2f} | T={target:.2f}")

    def close_position(self, token: str, exit_price: float, order_id: str) -> float:
        """
        Record position close. Returns realized P&L for this trade.
        """
        pos = self._positions.pop(token, None)
        if pos is None:
            logger.warning(f"[PositionMgr] No open position for token {token}")
            return 0.0

        qty = pos["qty"]
        if pos["direction"] == "long":
            pnl = (exit_price - pos["entry_price"]) * qty
        else:
            pnl = (pos["entry_price"] - exit_price) * qty

        pos["realized_pnl"] = pnl
        pos["exit_order_id"] = order_id
        self._realized_pnl  += pnl
        self._closed_trades.append(pos)

        emoji = "✅" if pnl >= 0 else "❌"
        logger.info(f"[PositionMgr] {emoji} Closed {pos['symbol']} | P&L=₹{pnl:.2f} "
                    f"| Entry={pos['entry_price']:.2f} Exit={exit_price:.2f}")
        return pnl

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def has_position(self, token: str) -> bool:
        return token in self._positions

    def get_position(self, token: str) -> dict | None:
        return self._positions.get(token)

    def get_all_positions(self) -> list[dict]:
        return list(self._positions.values())

    def open_count(self) -> int:
        return len(self._positions)

    @property
    def realized_pnl(self) -> float:
        return self._realized_pnl

    def unrealized_pnl(self, current_prices: dict[str, float]) -> float:
        """Compute unrealized P&L given current LTP per token."""
        total = 0.0
        for token, pos in self._positions.items():
            ltp = current_prices.get(token, pos["entry_price"])
            if pos["direction"] == "long":
                total += (ltp - pos["entry_price"]) * pos["qty"]
            else:
                total += (pos["entry_price"] - ltp) * pos["qty"]
        return total

    def session_summary(self) -> dict:
        return {
            "realized_pnl"   : round(self._realized_pnl, 2),
            "trades"         : len(self._closed_trades),
            "open_positions" : self.open_count(),
            "closed_trades"  : self._closed_trades,
        }

    def reset_session(self) -> None:
        self._positions     = {}
        self._realized_pnl  = 0.0
        self._closed_trades = []
