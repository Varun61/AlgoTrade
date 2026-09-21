"""
risk/position_sizer.py

Computes trade quantity based on:
  - Capital at risk per trade (% of total capital)
  - ATR-based stop distance
  - Lot size for derivatives (set to 1 for equity)

Formula:
    risk_amount  = capital * (per_trade_risk_pct / 100)
    stop_distance = abs(entry_price - stop_loss)
    quantity     = floor(risk_amount / stop_distance)

Usage:
    sizer = PositionSizer(capital=100000, per_trade_risk_pct=1.0)
    qty = sizer.compute_qty(entry_price=450.0, stop_loss=443.5)
"""

from __future__ import annotations
import logging, math

logger = logging.getLogger(__name__)


class PositionSizer:
    """
    ATR-based fixed-fractional position sizer.

    Args:
        capital             : Total trading capital in INR
        per_trade_risk_pct  : Fraction of capital to risk per trade (e.g. 1.0 = 1%)
        lot_size            : Lot size for derivatives (1 for equity)
        max_position_value  : Hard cap on single position value (safety guard)
    """

    def __init__(
        self,
        capital: float,
        per_trade_risk_pct: float = 1.0,
        lot_size: int = 1,
        max_position_value: float | None = None,
    ) -> None:
        self.capital            = capital
        self.per_trade_risk_pct = per_trade_risk_pct
        self.lot_size           = lot_size
        self.max_position_value = max_position_value or capital * 0.5  # Default: 50% of capital

    def compute_qty(self, entry_price: float, stop_loss: float) -> int:
        """
        Compute trade quantity.

        Returns 0 if the position would be too small or stop is invalid.
        """
        stop_distance = abs(entry_price - stop_loss)
        if stop_distance < 0.01:
            logger.warning("[Sizer] Stop distance near zero — returning qty=0")
            return 0

        risk_amount   = self.capital * (self.per_trade_risk_pct / 100)
        raw_qty       = risk_amount / stop_distance
        qty           = max(0, math.floor(raw_qty / self.lot_size) * self.lot_size)

        # Cap by max position value
        if qty > 0 and (qty * entry_price) > self.max_position_value:
            qty = math.floor(self.max_position_value / entry_price / self.lot_size) * self.lot_size

        logger.debug(f"[Sizer] Entry={entry_price:.2f}, SL={stop_loss:.2f}, "
                     f"Stop dist={stop_distance:.2f}, Risk=₹{risk_amount:.0f}, Qty={qty}")
        return qty

    def update_capital(self, new_capital: float) -> None:
        """Update capital after realized P&L (call at end of each trade)."""
        self.capital = new_capital
        logger.info(f"[Sizer] Capital updated to ₹{new_capital:,.0f}")
