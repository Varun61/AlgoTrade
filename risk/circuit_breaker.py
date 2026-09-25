"""
risk/circuit_breaker.py

Hard-stop risk controls. Sits between the signal engine and the order manager.
ALL orders must pass through the circuit breaker check before execution.

Controls:
  1. Daily loss limit (% of capital) — hard stop for the day
  2. Max trades per day cap
  3. Consecutive loss halt — pause + alert after N losses in a row
  4. Max concurrent open positions
"""

from __future__ import annotations
import logging
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class CircuitStatus(Enum):
    OK       = "OK"
    HALTED   = "HALTED"       # All trading paused
    WARN     = "WARN"         # Still trading but alert sent


@dataclass
class CircuitBreaker:
    """
    Stateful risk gate — reset at the start of each trading session.

    Args:
        capital               : Starting capital for today's session
        daily_loss_limit_pct  : Halt at this % daily loss (e.g. 2.0 = -2%)
        max_trades_per_day    : Hard cap on number of completed trades (None/0 = unlimited)
        max_concurrent        : Max simultaneous open positions
        max_consecutive_losses: Pause after this many losses in a row
    """
    capital                : float
    daily_loss_limit_pct   : float = 2.0
    max_trades_per_day     : int | None = 10
    max_concurrent         : int   = 3
    max_consecutive_losses : int   = 4

    # Runtime state (reset at session start)
    realized_pnl           : float = field(default=0.0, init=False)
    trades_today           : int   = field(default=0,   init=False)
    consecutive_losses     : int   = field(default=0,   init=False)
    open_positions         : int   = field(default=0,   init=False)
    status                 : CircuitStatus = field(default=CircuitStatus.OK, init=False)
    halt_reason            : str   = field(default="",  init=False)

    # ------------------------------------------------------------------
    # Gate check — call before every new order
    # ------------------------------------------------------------------

    def can_trade(self) -> tuple[bool, str]:
        """
        Returns (True, "") if a new trade is allowed.
        Returns (False, reason_string) if blocked.
        """
        if self.status == CircuitStatus.HALTED:
            return False, f"Circuit HALTED: {self.halt_reason}"

        # Daily loss limit
        loss_pct = (-self.realized_pnl / self.capital) * 100
        if loss_pct >= self.daily_loss_limit_pct:
            self._halt(f"Daily loss limit hit: -{loss_pct:.2f}% >= -{self.daily_loss_limit_pct}%")
            return False, self.halt_reason

        # Max trades
        if self.max_trades_per_day and self.trades_today >= self.max_trades_per_day:
            return False, f"Max trades/day reached: {self.trades_today}"

        # Max concurrent positions
        if self.open_positions >= self.max_concurrent:
            return False, f"Max concurrent positions: {self.open_positions}/{self.max_concurrent}"

        # Consecutive loss halt
        if self.consecutive_losses >= self.max_consecutive_losses:
            self._halt(f"Consecutive losses: {self.consecutive_losses}")
            return False, self.halt_reason

        return True, ""

    # ------------------------------------------------------------------
    # State update methods — call from position_manager / order_tracker
    # ------------------------------------------------------------------

    def on_trade_open(self) -> None:
        self.open_positions += 1

    def on_trade_close(self, pnl: float) -> None:
        self.realized_pnl    += pnl
        self.trades_today    += 1
        self.open_positions   = max(0, self.open_positions - 1)

        if pnl < 0:
            self.consecutive_losses += 1
            logger.warning(f"[CircuitBreaker] Loss trade #{self.consecutive_losses} "
                           f"in a row | P&L=₹{pnl:.2f}")
        else:
            if self.consecutive_losses > 0:
                logger.info(f"[CircuitBreaker] Win — resetting consecutive loss counter "
                            f"(was {self.consecutive_losses})")
            self.consecutive_losses = 0

        logger.info(f"[CircuitBreaker] Daily P&L=₹{self.realized_pnl:.2f} "
                    f"| Trades={self.trades_today} | ConsecLoss={self.consecutive_losses}")

    def manual_resume(self) -> None:
        """Allow operator to manually resume after a consecutive-loss halt."""
        if self.status == CircuitStatus.HALTED and "Consecutive losses" in self.halt_reason:
            self.consecutive_losses = 0
            self.status             = CircuitStatus.OK
            self.halt_reason        = ""
            logger.info("[CircuitBreaker] Manually resumed.")
        else:
            logger.warning("[CircuitBreaker] Cannot resume — halt reason: {self.halt_reason}")

    def reset_session(self, new_capital: float | None = None) -> None:
        """Call at the start of each trading day."""
        if new_capital:
            self.capital = new_capital
        self.realized_pnl       = 0.0
        self.trades_today       = 0
        self.consecutive_losses = 0
        self.open_positions     = 0
        self.status             = CircuitStatus.OK
        self.halt_reason        = ""
        logger.info("[CircuitBreaker] Session reset.")

    @property
    def daily_pnl_pct(self) -> float:
        return (self.realized_pnl / self.capital) * 100

    # ------------------------------------------------------------------
    def _halt(self, reason: str) -> None:
        self.status      = CircuitStatus.HALTED
        self.halt_reason = reason
        logger.critical(f"[CircuitBreaker] 🛑 HALTED: {reason}")
