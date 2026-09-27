"""
strategy/swing.py

DailySwingStrategy — a positional (multi-day) strategy for DAILY bars.

Rationale: the 15-min experiments showed intraday move-size can't beat cost drag.
A swing hold captures multi-day % moves where costs are a small fraction of the
move. This uses the most documented daily equity edge — a short-term pullback in
the direction of the longer-term trend (a la Connors RSI(2)):

  LONG  : close > SMA(trend)  AND  RSI(2) <= rsi_entry     (buy the dip in an uptrend)
  exit  : close > SMA(exit_sma)  OR  RSI(2) >= rsi_exit  OR  ATR stop  OR  max_holding_days
  SHORT (optional, `allow_short`): mirror below the trend SMA.

Signal-based exit (no fixed price target) — `get_current_target()` returns None,
so the backtester relies on the ATR stop (intrabar) plus the on_candle_close exit
signal. Same StrategyBase interface as the other strategies, so it drops into the
portfolio backtester in swing mode.
"""

from __future__ import annotations
import logging

import pandas as pd

from .strategy_base import StrategyBase, TradeSignal, Signal
from .indicators import rsi, atr

logger = logging.getLogger(__name__)


class DailySwingStrategy(StrategyBase):
    """
    Config keys:
        trend_sma       : int   = 50    (long-term trend filter period)
        exit_sma        : int   = 5     (exit when close crosses back above/below this)
        rsi_period      : int   = 2     (short pullback oscillator)
        rsi_entry       : float = 15.0  (long entry when RSI <= this; short when >= 100-this)
        rsi_exit        : float = 70.0  (long exit when RSI >= this; short when <= 100-this)
        atr_period      : int   = 14
        atr_stop_mult   : float = 3.0   (wide stop — daily swings need room)
        max_holding_days: int   = 10
        allow_short     : bool  = False
        min_confidence  : float = 0.0
        min_price       : float = 50.0  (skip penny/illiquid names below this price)
    """

    def __init__(self, symbol: str, token: str, **kwargs) -> None:
        super().__init__(symbol, token, **kwargs)
        p = kwargs
        self.trend_sma      = int(p.get("trend_sma", 50))
        self.exit_sma       = int(p.get("exit_sma", 5))
        self.rsi_period     = int(p.get("rsi_period", 2))
        self.rsi_entry      = float(p.get("rsi_entry", 15.0))
        self.rsi_exit       = float(p.get("rsi_exit", 70.0))
        self.atr_period     = int(p.get("atr_period", 14))
        self.atr_stop_mult  = float(p.get("atr_stop_mult", 3.0))
        self.max_holding_days = int(p.get("max_holding_days", 10))
        self.allow_short    = bool(p.get("allow_short", False))
        self.min_confidence = float(p.get("min_confidence", 0.0))
        self.min_price      = float(p.get("min_price", 50.0))

        self._position = None
        self._entry_price = 0.0
        self._stop_loss = 0.0
        self._held = 0

    def reset(self) -> None:
        self._position = None
        self._entry_price = 0.0
        self._stop_loss = 0.0
        self._held = 0

    def force_exit(self) -> None:
        self.reset()

    def discard_pending_entry(self) -> None:
        self.reset()

    def get_current_stop(self):
        return self._stop_loss if self._position is not None else None

    def get_current_target(self):
        return None  # signal-based exit, no fixed price target

    def get_position_direction(self):
        return self._position

    def get_entry_price(self):
        return self._entry_price if self._position is not None else None

    def set_sector_momentum(self, pct: float) -> None:
        pass

    # ---------------------------------------------------------------
    def on_candle_close(self, candle: pd.Series, history: pd.DataFrame) -> TradeSignal:
        hold = TradeSignal(signal=Signal.HOLD, symbol=self.symbol, token=self.token)
        n = len(history)
        if n < max(self.trend_sma + 2, self.atr_period + 2, self.rsi_period + 2):
            return hold

        closes = history["close"]
        highs = history["high"]
        lows = history["low"]
        close = float(candle["close"])
        sma_t = float(closes.rolling(self.trend_sma).mean().iloc[-1])
        sma_x = float(closes.rolling(self.exit_sma).mean().iloc[-1])
        rsi_v = float(rsi(closes, self.rsi_period).iloc[-1])
        atr_v = float(atr(highs, lows, closes, self.atr_period).iloc[-1])

        # === Exit management ===
        if self._position is not None:
            self._held += 1
            if self._position == "long":
                if close <= self._stop_loss:
                    return self._exit(Signal.EXIT_LONG, f"ATR stop: ₹{close:.2f} ≤ ₹{self._stop_loss:.2f}")
                if close > sma_x or rsi_v >= self.rsi_exit:
                    return self._exit(Signal.EXIT_LONG, f"Swing exit: close>SMA{self.exit_sma} or RSI≥{self.rsi_exit:.0f}")
                if self.max_holding_days and self._held >= self.max_holding_days:
                    return self._exit(Signal.EXIT_LONG, f"Max holding {self.max_holding_days}d")
            else:
                if close >= self._stop_loss:
                    return self._exit(Signal.EXIT_SHORT, f"ATR stop: ₹{close:.2f} ≥ ₹{self._stop_loss:.2f}")
                if close < sma_x or rsi_v <= (100 - self.rsi_exit):
                    return self._exit(Signal.EXIT_SHORT, f"Swing exit: close<SMA{self.exit_sma} or RSI≤{100-self.rsi_exit:.0f}")
                if self.max_holding_days and self._held >= self.max_holding_days:
                    return self._exit(Signal.EXIT_SHORT, f"Max holding {self.max_holding_days}d")
            return hold

        # === Entry ===
        if close < self.min_price or atr_v <= 0 or pd.isna(sma_t) or pd.isna(rsi_v):
            return hold

        # LONG: pullback in an uptrend
        if close > sma_t and rsi_v <= self.rsi_entry:
            sl = close - self.atr_stop_mult * atr_v
            score = min(100.0, (self.rsi_entry - rsi_v) / max(self.rsi_entry, 1e-9) * 100)
            if score < self.min_confidence:
                return hold
            self._position, self._entry_price, self._stop_loss, self._held = "long", close, sl, 0
            return TradeSignal(signal=Signal.BUY, symbol=self.symbol, token=self.token,
                               entry_price=close, stop_loss=round(sl, 2), target=0.0,
                               atr=round(atr_v, 2), confidence=round(score, 1),
                               reason=f"LONG swing | close>SMA{self.trend_sma} pullback RSI{self.rsi_period}={rsi_v:.0f}")

        # SHORT: bounce in a downtrend (optional)
        if self.allow_short and close < sma_t and rsi_v >= (100 - self.rsi_entry):
            sl = close + self.atr_stop_mult * atr_v
            score = min(100.0, (rsi_v - (100 - self.rsi_entry)) / max(self.rsi_entry, 1e-9) * 100)
            if score < self.min_confidence:
                return hold
            self._position, self._entry_price, self._stop_loss, self._held = "short", close, sl, 0
            return TradeSignal(signal=Signal.SELL, symbol=self.symbol, token=self.token,
                               entry_price=close, stop_loss=round(sl, 2), target=0.0,
                               atr=round(atr_v, 2), confidence=round(score, 1),
                               reason=f"SHORT swing | close<SMA{self.trend_sma} bounce RSI{self.rsi_period}={rsi_v:.0f}")

        return hold

    def _exit(self, sig: Signal, reason: str) -> TradeSignal:
        entry = self._entry_price
        self._position = None
        return TradeSignal(signal=sig, symbol=self.symbol, token=self.token,
                           entry_price=entry, reason=reason)
