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
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Zerodha equity intraday (MIS) rate card, verified against
# https://zerodha.com/brokerage-calculator. Real round-trip cost is ~2x
# brokerage alone once STT/exchange/SEBI/GST/stamp are included — a
# brokerage-only estimate materially understates cost drag.
_BROKERAGE_RATE = 0.0003     # 0.03%, per executed order (both legs)
_BROKERAGE_CAP  = 20.0       # ₹20 per executed order
_STT_RATE       = 0.00025    # 0.025%, sell side only
_EXCHANGE_RATE  = 0.0000322  # 0.00322%, total turnover
_SEBI_RATE      = 0.000001   # ₹10/crore, total turnover
_GST_RATE       = 0.18       # on brokerage + exchange + SEBI
_STAMP_RATE     = 0.00003    # 0.003%, buy side only


@dataclass
class CostBreakdown:
    brokerage: float
    stt: float
    exchange: float
    sebi: float
    gst: float
    stamp: float

    @property
    def total(self) -> float:
        return self.brokerage + self.stt + self.exchange + self.sebi + self.gst + self.stamp


def zerodha_intraday_costs(buy_value: float, sell_value: float) -> CostBreakdown:
    """Full round-trip cost for one buy + one sell of the given notional values."""
    brokerage = min(buy_value * _BROKERAGE_RATE, _BROKERAGE_CAP) + \
                min(sell_value * _BROKERAGE_RATE, _BROKERAGE_CAP)
    stt       = sell_value * _STT_RATE
    turnover  = buy_value + sell_value
    exchange  = turnover * _EXCHANGE_RATE
    sebi      = turnover * _SEBI_RATE
    gst       = _GST_RATE * (brokerage + exchange + sebi)
    stamp     = buy_value * _STAMP_RATE
    return CostBreakdown(brokerage, stt, exchange, sebi, gst, stamp)


# Zerodha DELIVERY (CNC) equity rate card — for multi-day swing holds. Differs
# materially from intraday: zero brokerage, but STT is 0.1% on BOTH legs (vs
# 0.025% sell-only intraday), stamp 0.015% buy-side, plus a flat DP charge on the
# sell leg. Verified against https://zerodha.com/brokerage-calculator.
_DELIVERY_BROKERAGE = 0.0        # Zerodha CNC delivery = free brokerage
_DELIVERY_STT_RATE  = 0.001      # 0.1% on buy AND sell
_DELIVERY_STAMP     = 0.00015    # 0.015%, buy side only
_DP_CHARGE_SELL     = 15.34      # flat ₹ per scrip on the sell leg (incl. GST)


def zerodha_delivery_costs(buy_value: float, sell_value: float) -> CostBreakdown:
    """Full round-trip cost for a delivery (CNC) buy + sell — for swing trades."""
    brokerage = _DELIVERY_BROKERAGE
    stt       = (buy_value + sell_value) * _DELIVERY_STT_RATE
    turnover  = buy_value + sell_value
    exchange  = turnover * _EXCHANGE_RATE
    sebi      = turnover * _SEBI_RATE
    gst       = _GST_RATE * (brokerage + exchange + sebi)
    stamp     = buy_value * _DELIVERY_STAMP
    # Fold the flat DP sell charge into the "brokerage" bucket (it's the only
    # flat, non-turnover component) so CostBreakdown.total stays correct.
    brokerage += _DP_CHARGE_SELL
    return CostBreakdown(brokerage, stt, exchange, sebi, gst, stamp)


def expected_breakeven_move(entry_price: float, qty: int) -> float:
    """Minimum % move (in either direction) needed just to clear round-trip costs."""
    notional = entry_price * qty
    if notional <= 0:
        return 0.0
    return zerodha_intraday_costs(notional, notional).total / notional * 100


class PositionManager:
    """Maintains the current open positions and realized P&L for the session."""

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
        Record position close. Returns realized P&L (net of brokerage) for this trade.
        """
        pos = self._positions.pop(token, None)
        if pos is None:
            logger.warning(f"[PositionMgr] No open position for token {token}")
            return 0.0

        qty = pos["qty"]
        if pos["direction"] == "long":
            gross_pnl = (exit_price - pos["entry_price"]) * qty
        else:
            gross_pnl = (pos["entry_price"] - exit_price) * qty

        buy_value  = pos["entry_price"] * qty if pos["direction"] == "long" else exit_price * qty
        sell_value = exit_price * qty if pos["direction"] == "long" else pos["entry_price"] * qty
        costs = zerodha_intraday_costs(buy_value, sell_value)
        pnl = gross_pnl - costs.total

        pos["gross_pnl"]      = gross_pnl
        pos["costs"]          = costs.total
        pos["realized_pnl"]   = pnl
        pos["exit_order_id"]  = order_id
        self._realized_pnl   += pnl
        self._closed_trades.append(pos)

        emoji = "✅" if pnl >= 0 else "❌"
        logger.info(f"[PositionMgr] {emoji} Closed {pos['symbol']} | P&L=₹{pnl:.2f} (gross ₹{gross_pnl:.2f} − costs ₹{costs.total:.2f}: "
                    f"brokerage ₹{costs.brokerage:.2f}, STT ₹{costs.stt:.2f}, exchange ₹{costs.exchange:.2f}, "
                    f"SEBI ₹{costs.sebi:.4f}, GST ₹{costs.gst:.2f}, stamp ₹{costs.stamp:.2f}) "
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


def find_weakest_position(
    positions: list[dict],
    current_ltps: dict[str, float],
    live_stops: dict[str, float] | None = None,
) -> str | None:
    """
    Token of the open position closest to hitting its own stop-loss (smallest
    fraction of initial risk remaining) — used to pick a rotation candidate to
    close out in favor of a stronger new setup.

    live_stops optionally supplies the strategy's current (post-breakeven/
    trailing) stop per token; falls back to the position's static entry-time
    stop_loss when not provided/present for a token.
    """
    live_stops = live_stops or {}
    weakest_token, weakest_frac = None, None
    for pos in positions:
        token = pos["token"]
        ltp = current_ltps.get(token, pos["entry_price"])
        stop = live_stops.get(token, pos["stop_loss"])
        risk = abs(pos["entry_price"] - stop)
        if risk <= 0:
            continue
        if pos["direction"] == "long":
            frac_remaining = (ltp - stop) / risk
        else:
            frac_remaining = (stop - ltp) / risk
        if weakest_frac is None or frac_remaining < weakest_frac:
            weakest_token, weakest_frac = token, frac_remaining
    return weakest_token

