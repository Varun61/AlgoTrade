"""
strategy/strategy_base.py

Abstract base class for all strategies.

Key principle: strategies emit Signal objects only.
They never call the API, place orders, or read live prices directly.
The orchestrator (main.py) translates signals into notification/execution actions.
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import pandas as pd


class Signal(Enum):
    HOLD        = "HOLD"
    BUY         = "BUY"          # Enter long (high-confidence alert)
    SELL        = "SELL"         # Enter short (high-confidence alert)
    EXIT_LONG   = "EXIT_LONG"    # Close long position
    EXIT_SHORT  = "EXIT_SHORT"   # Close short position


@dataclass
class TradeSignal:
    """
    Rich signal object emitted by strategies.

    Attributes:
        signal             : Signal enum value
        symbol             : Trading symbol (e.g. "SBIN-EQ")
        token              : Instrument token
        entry_price        : Suggested entry price (0 = use market)
        stop_loss          : Stop loss price
        target             : Target price
        atr                : Current ATR (used by position sizer)
        reason             : Human-readable reason string for logging
        confidence         : 0–100 score. Only notify if >= threshold (default 70)
        confidence_factors : Dict of individual factor scores for transparency
        rr_ratio           : Risk:reward ratio (auto-computed if not set)
        entry_window_mins  : How long this alert remains valid (minutes)
    """
    signal             : Signal
    symbol             : str   = ""
    token              : str   = ""
    entry_price        : float = 0.0
    stop_loss          : float = 0.0
    target             : float = 0.0
    atr                : float = 0.0
    reason             : str   = ""
    confidence         : float = 0.0
    confidence_factors : dict  = field(default_factory=dict)
    rr_ratio           : float = 0.0
    entry_window_mins  : int   = 15   # Alert valid for next N candle minutes

    def __post_init__(self):
        # Auto-compute R:R if not provided
        if self.rr_ratio == 0.0 and self.stop_loss and self.target and self.entry_price:
            risk   = abs(self.entry_price - self.stop_loss)
            reward = abs(self.target - self.entry_price)
            if risk > 0:
                self.rr_ratio = round(reward / risk, 2)

    def is_entry(self) -> bool:
        return self.signal in (Signal.BUY, Signal.SELL)

    def is_exit(self) -> bool:
        return self.signal in (Signal.EXIT_LONG, Signal.EXIT_SHORT)

    def confidence_label(self) -> str:
        """Human-readable confidence tier."""
        if self.confidence >= 85:
            return "🔥 VERY HIGH"
        elif self.confidence >= 70:
            return "✅ HIGH"
        elif self.confidence >= 55:
            return "⚠️ MODERATE"
        else:
            return "❌ LOW"


class StrategyBase(ABC):
    """
    Abstract interface for all trading strategies.

    Subclasses implement `on_candle_close()` which receives:
      - candle  : The just-closed candle as a pd.Series
      - history : All candles (including the closed one), sorted ascending

    The strategy returns a TradeSignal. The orchestrator decides whether to
    notify / execute based on confidence threshold.
    """

    def __init__(self, symbol: str, token: str, **kwargs) -> None:
        self.symbol  = symbol
        self.token   = token
        self.params  = kwargs

    @abstractmethod
    def on_candle_close(self, candle: pd.Series, history: pd.DataFrame) -> TradeSignal:
        """
        Called once per closed candle.

        Args:
            candle  : Latest closed candle (open, high, low, close, volume, timestamp)
            history : All candles up to and including `candle` (sorted ascending)

        Returns:
            TradeSignal — strategy's recommendation (check .confidence before acting)
        """
        ...

    def on_position_update(self, position: dict) -> TradeSignal:
        """
        Optional hook: called when an open position update is received.
        Default: HOLD.
        """
        return TradeSignal(signal=Signal.HOLD, symbol=self.symbol, token=self.token)

    def reset(self) -> None:
        """Reset internal state (called at start of each trading session)."""
        pass

    def force_exit(self) -> None:
        """
        Externally force-close any tracked position without emitting a signal.

        Called by the backtest engine (and can be called live) after a hard
        stop/target/EOD close is executed outside the strategy's own
        on_candle_close() path, so the strategy's internal position state
        stays in sync with the caller's. Default: no-op (stateless strategies).
        """
        pass

    def get_current_stop(self) -> float | None:
        """
        Current live stop-loss for an open position (post breakeven/trailing
        updates), or None if no position or the strategy doesn't manage a
        moving stop. Callers use this to keep an externally-tracked hard
        stop level in sync with a strategy's internal trailing logic.
        """
        return None

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(symbol={self.symbol})"
