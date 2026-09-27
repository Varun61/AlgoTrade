"""
strategy/vwap_reversion.py

VWAP MEAN-REVERSION strategy.

Rationale (from the ORB backtest post-mortem): on this ~300-name NSE universe on
15-min bars, price tends to REVERT toward the session VWAP intraday rather than
trend away from the open — which is exactly why the ORB breakout strategy had a
negative edge in every quarter tested. This strategy makes the opposite bet:

  - When price is stretched FAR ABOVE the session VWAP (by >= band_atr * ATR) and
    momentum is overbought (RSI high) in a NON-trending regime (ADX low) → SHORT,
    targeting a reversion back toward VWAP.
  - When price is stretched FAR BELOW VWAP and oversold in a range regime → LONG.

It deliberately reuses the SAME StrategyBase interface (on_candle_close +
get_current_stop/target/position/entry, force_exit, reset, discard_pending_entry)
so it drops straight into the existing portfolio backtester and, if it ever earns
an edge, the live orchestrator — with the same risk engine and cost model.

Key differences from ORBEMAVWAPStrategy:
  - Regime filter is INVERTED: mean-reversion wants LOW ADX (chop), not high.
  - Target is the VWAP level (dynamic, reversion target), not a fixed ATR multiple.
  - Entry is a stretch/extension, not a breakout.

Nothing here talks to the API — it only emits TradeSignal objects.
"""

from __future__ import annotations
import logging
from datetime import datetime

import pandas as pd

from .strategy_base import StrategyBase, TradeSignal, Signal
from .indicators import ema, rsi, atr, vwap, adx

logger = logging.getLogger(__name__)

MIN_CONFIDENCE_THRESHOLD = 60


class VWAPMeanReversionStrategy(StrategyBase):
    """
    VWAP mean-reversion with ADX(range)/RSI(extreme)/volatility gates.

    Config keys (via **kwargs or settings.yaml strategy block):
        candle_minutes    : int   = 15
        atr_period        : int   = 14
        rsi_period        : int   = 14
        adx_period        : int   = 14
        band_atr          : float = 1.5   (enter when |close-VWAP| >= this * ATR)
        stop_atr_mult     : float = 1.0   (stop this many ATR beyond entry, away from VWAP)
        target_frac       : float = 0.8   (take profit at this fraction of the way back to VWAP)
        max_adx           : float = 25.0  (skip if ADX above this — market is trending, not ranging)
        rsi_long_max      : float = 35.0  (long only if RSI at/below this — oversold)
        rsi_short_min     : float = 65.0  (short only if RSI at/above this — overbought)
        min_confidence    : float = 60.0
        max_holding_candles: int  = 12
        min_atr_pct       : float = 0.1   (skip too-illiquid/flat names)
        max_atr_pct       : float = 3.0   (skip too-wild names)
        min_entry_time    : str   = "10:00" (let VWAP stabilize after the open)
        min_vwap_bars     : int   = 6    (require at least N of today's bars before trading VWAP)
        vwap_filter etc.  : ignored (accepted for config compatibility)
    """

    def __init__(self, symbol: str, token: str, **kwargs) -> None:
        super().__init__(symbol, token, **kwargs)
        p = kwargs
        self.candle_minutes = int(p.get("candle_minutes", 15))
        self.atr_period     = int(p.get("atr_period", 14))
        self.rsi_period     = int(p.get("rsi_period", 14))
        self.adx_period     = int(p.get("adx_period", 14))
        self.band_atr       = float(p.get("band_atr", 1.5))
        self.stop_atr_mult  = float(p.get("stop_atr_mult", 1.0))
        self.target_frac    = float(p.get("target_frac", 0.8))
        self.max_adx        = float(p.get("max_adx", 25.0))
        self.rsi_long_max   = float(p.get("rsi_long_max", 35.0))
        self.rsi_short_min  = float(p.get("rsi_short_min", 65.0))
        self.min_confidence = float(p.get("min_confidence", MIN_CONFIDENCE_THRESHOLD))
        self.max_holding_candles = int(p.get("max_holding_candles", 12))
        self.min_atr_pct    = float(p.get("min_atr_pct", 0.1))
        self.max_atr_pct    = float(p.get("max_atr_pct", 3.0))
        self.min_vwap_bars  = int(p.get("min_vwap_bars", 6))
        met = p.get("min_entry_time") or None
        self.min_entry_time = datetime.strptime(met, "%H:%M").time() if met else None

        self._position    = None      # None | "long" | "short"
        self._entry_price = 0.0
        self._stop_loss   = 0.0
        self._target      = 0.0
        self._candles_held = 0

    # --- StrategyBase interface used by the backtester / live loop ---
    def reset(self) -> None:
        self._position = None
        self._entry_price = 0.0
        self._stop_loss = 0.0
        self._target = 0.0
        self._candles_held = 0

    def force_exit(self) -> None:
        self.reset()

    def discard_pending_entry(self) -> None:
        self.reset()

    def get_current_stop(self):
        return self._stop_loss if self._position is not None else None

    def get_current_target(self):
        return self._target if self._position is not None else None

    def get_position_direction(self):
        return self._position

    def get_entry_price(self):
        return self._entry_price if self._position is not None else None

    def set_sector_momentum(self, pct: float) -> None:  # accepted, unused here
        pass

    # ---------------------------------------------------------------
    def on_candle_close(self, candle: pd.Series, history: pd.DataFrame) -> TradeSignal:
        hold = TradeSignal(signal=Signal.HOLD, symbol=self.symbol, token=self.token)

        n = len(history)
        min_bars = max(self.atr_period + 2, self.rsi_period + 2, self.adx_period + 2, 20)
        if n < min_bars:
            return hold

        closes  = history["close"]
        highs   = history["high"]
        lows    = history["low"]
        volumes = history["volume"]

        atr_val = float(atr(highs, lows, closes, self.atr_period).iloc[-1])
        rsi_val = float(rsi(closes, self.rsi_period).iloc[-1])
        adx_val = adx(highs, lows, closes, self.adx_period).iloc[-1]
        close   = float(candle["close"])

        # Intraday VWAP (reset daily) — re-index by timestamp so vwap() groups by day.
        if "timestamp" in history.columns:
            dt_idx = pd.to_datetime(history["timestamp"], errors="coerce")
            vwap_series = vwap(highs.set_axis(dt_idx), lows.set_axis(dt_idx),
                               closes.set_axis(dt_idx), volumes.set_axis(dt_idx))
            candle_ts = pd.to_datetime(candle.get("timestamp"), errors="coerce")
            today_bars = int((dt_idx.dt.date == candle_ts.date()).sum()) if pd.notna(candle_ts) else n
        else:
            vwap_series = vwap(highs, lows, closes, volumes)
            candle_ts = None
            today_bars = n
        vwap_v = float(vwap_series.iloc[-1])

        # === EXIT management for an open position ===
        if self._position is not None:
            return self._check_exits(close)

        # === Entry gates ===
        if today_bars < self.min_vwap_bars:
            return hold
        if self.min_entry_time is not None and candle_ts is not None and candle_ts.time() < self.min_entry_time:
            return hold
        if atr_val <= 0:
            return hold
        atr_pct = atr_val / close * 100 if close else 0.0
        if not (self.min_atr_pct <= atr_pct <= self.max_atr_pct):
            return hold
        # Mean reversion wants a RANGING market — skip strong trends.
        if pd.isna(adx_val) or adx_val > self.max_adx:
            return hold

        dist_atr = (close - vwap_v) / atr_val   # +ve = above VWAP (short candidate)

        # --- SHORT: stretched above VWAP + overbought ---
        if dist_atr >= self.band_atr and rsi_val >= self.rsi_short_min:
            target = close - self.target_frac * (close - vwap_v)   # revert toward VWAP
            sl     = close + self.stop_atr_mult * atr_val
            score, factors = self._score(dist_atr, rsi_val, adx_val, "short")
            if score < self.min_confidence:
                return hold
            self._position, self._entry_price = "short", close
            self._stop_loss, self._target, self._candles_held = sl, target, 0
            return TradeSignal(
                signal=Signal.SELL, symbol=self.symbol, token=self.token,
                entry_price=close, stop_loss=round(sl, 2), target=round(target, 2),
                atr=round(atr_val, 2), confidence=round(score, 1), confidence_factors=factors,
                entry_window_mins=self.candle_minutes,
                reason=(f"SHORT mean-revert | {dist_atr:.2f} ATR above VWAP ₹{vwap_v:.2f} | "
                        f"RSI={rsi_val:.0f} ADX={adx_val:.0f} | target VWAP"),
            )

        # --- LONG: stretched below VWAP + oversold ---
        if dist_atr <= -self.band_atr and rsi_val <= self.rsi_long_max:
            target = close + self.target_frac * (vwap_v - close)
            sl     = close - self.stop_atr_mult * atr_val
            score, factors = self._score(dist_atr, rsi_val, adx_val, "long")
            if score < self.min_confidence:
                return hold
            self._position, self._entry_price = "long", close
            self._stop_loss, self._target, self._candles_held = sl, target, 0
            return TradeSignal(
                signal=Signal.BUY, symbol=self.symbol, token=self.token,
                entry_price=close, stop_loss=round(sl, 2), target=round(target, 2),
                atr=round(atr_val, 2), confidence=round(score, 1), confidence_factors=factors,
                entry_window_mins=self.candle_minutes,
                reason=(f"LONG mean-revert | {abs(dist_atr):.2f} ATR below VWAP ₹{vwap_v:.2f} | "
                        f"RSI={rsi_val:.0f} ADX={adx_val:.0f} | target VWAP"),
            )

        return hold

    # ---------------------------------------------------------------
    def _check_exits(self, close: float) -> TradeSignal:
        self._candles_held += 1
        if self._position == "long":
            if close <= self._stop_loss:
                return self._exit(Signal.EXIT_LONG, f"Stop hit: ₹{close:.2f} ≤ SL ₹{self._stop_loss:.2f}")
            if close >= self._target:
                return self._exit(Signal.EXIT_LONG, f"Target hit (VWAP revert): ₹{close:.2f} ≥ ₹{self._target:.2f}")
            if self.max_holding_candles and self._candles_held >= self.max_holding_candles:
                return self._exit(Signal.EXIT_LONG, f"Max holding ({self.max_holding_candles}) reached")
        else:  # short
            if close >= self._stop_loss:
                return self._exit(Signal.EXIT_SHORT, f"Stop hit: ₹{close:.2f} ≥ SL ₹{self._stop_loss:.2f}")
            if close <= self._target:
                return self._exit(Signal.EXIT_SHORT, f"Target hit (VWAP revert): ₹{close:.2f} ≤ ₹{self._target:.2f}")
            if self.max_holding_candles and self._candles_held >= self.max_holding_candles:
                return self._exit(Signal.EXIT_SHORT, f"Max holding ({self.max_holding_candles}) reached")
        return TradeSignal(signal=Signal.HOLD, symbol=self.symbol, token=self.token)

    def _exit(self, sig: Signal, reason: str) -> TradeSignal:
        entry = self._entry_price
        self._position = None
        return TradeSignal(signal=sig, symbol=self.symbol, token=self.token,
                           entry_price=entry, reason=reason)

    # ---------------------------------------------------------------
    def _score(self, dist_atr: float, rsi_val: float, adx_val: float, side: str) -> tuple[float, dict]:
        """0-100 confidence: more stretch, more RSI extremity, lower ADX (more range) = better."""
        factors = {}
        # 1. Stretch beyond the band, in ATR units (band .. band+2 ATR -> 0..40).
        excess = max(0.0, abs(dist_atr) - self.band_atr)
        factors["stretch"] = round(min(40.0, excess / 2.0 * 40.0), 1)
        # 2. RSI extremity (0..30).
        if side == "short":
            rsi_ext = max(0.0, rsi_val - self.rsi_short_min) / (100 - self.rsi_short_min + 1e-9)
        else:
            rsi_ext = max(0.0, self.rsi_long_max - rsi_val) / (self.rsi_long_max + 1e-9)
        factors["rsi_extreme"] = round(min(30.0, rsi_ext * 30.0), 1)
        # 3. Range regime — lower ADX is better for reversion (0..30).
        adx_v = 0.0 if pd.isna(adx_val) else adx_val
        factors["range_regime"] = round(min(30.0, max(0.0, (self.max_adx - adx_v) / self.max_adx * 30.0)), 1)
        return min(100.0, sum(factors.values())), factors
