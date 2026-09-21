"""
strategy/signal_engine.py

ORB + EMA crossover + VWAP filter + RSI filter + ATR stops
WITH CONFIDENCE SCORING (0-100).

Confidence is built from 6 independent factors, each scored 0-20:
  1. ORB breakout strength   — how decisively price cleared the range
  2. EMA alignment           — separation between fast/slow EMAs (trend strength)
  3. VWAP position           — price distance from VWAP (momentum)
  4. RSI quality             — RSI in ideal entry zone (not overbought/oversold)
  5. Volume confirmation     — current volume vs. average volume
  6. Risk:Reward ratio       — must be >= 1.5 to score; higher is better

Only signals with confidence >= MIN_CONFIDENCE_THRESHOLD are emitted as BUY/SELL.
Below threshold: HOLD is returned (not worth alerting).

Design: automatic order execution is a PLACEHOLDER — see _execute_placeholder().
"""

from __future__ import annotations
import logging

import numpy as np
import pandas as pd

from .strategy_base import StrategyBase, TradeSignal, Signal
from .indicators    import ema, rsi, atr, vwap, opening_range

logger = logging.getLogger(__name__)

# Only notify above this score (0-100)
MIN_CONFIDENCE_THRESHOLD = 70


class ORBEMAVWAPStrategy(StrategyBase):
    """
    Opening Range Breakout with EMA/VWAP/RSI/Volume confirmation + confidence score.

    Config keys (via **kwargs or settings.yaml):
        orb_minutes          : int   = 15
        ema_fast             : int   = 9
        ema_slow             : int   = 21
        rsi_period           : int   = 14
        rsi_overbought       : int   = 70
        rsi_oversold         : int   = 30
        atr_period           : int   = 14
        atr_stop_mult        : float = 1.5
        atr_target_mult      : float = 2.5
        vwap_filter          : bool  = True
        candle_minutes       : int   = 15
        min_confidence       : float = 70.0   (override threshold per strategy instance)
        volume_avg_periods   : int   = 20     (periods to compute average volume)
    """

    def __init__(self, symbol: str, token: str, **kwargs) -> None:
        super().__init__(symbol, token, **kwargs)
        p = kwargs

        self.candle_minutes    = int(p.get("candle_minutes",    15))
        self.orb_minutes       = int(p.get("orb_minutes",       15))
        self.ema_fast          = int(p.get("ema_fast",           9))
        self.ema_slow          = int(p.get("ema_slow",          21))
        self.rsi_period        = int(p.get("rsi_period",        14))
        self.rsi_overbought    = int(p.get("rsi_overbought",    70))
        self.rsi_oversold      = int(p.get("rsi_oversold",      30))
        self.atr_period        = int(p.get("atr_period",        14))
        self.atr_stop_mult     = float(p.get("atr_stop_mult",   1.5))
        self.atr_target_mult   = float(p.get("atr_target_mult", 2.5))
        self.vwap_filter       = bool(p.get("vwap_filter",      True))
        self.min_confidence    = float(p.get("min_confidence",  MIN_CONFIDENCE_THRESHOLD))
        self.vol_avg_periods   = int(p.get("volume_avg_periods", 20))
        self.orb_candles       = max(1, self.orb_minutes // self.candle_minutes)

        # Session state
        self._position         = None     # None | "long" | "short"
        self._entry_price      = 0.0
        self._stop_loss        = 0.0
        self._target           = 0.0
        self._orb_high         = None
        self._orb_low          = None

    def reset(self) -> None:
        self._position   = None
        self._entry_price = 0.0
        self._stop_loss   = 0.0
        self._target      = 0.0
        self._orb_high    = None
        self._orb_low     = None

    # ---------------------------------------------------------------
    # Main candle handler
    # ---------------------------------------------------------------

    def on_candle_close(self, candle: pd.Series, history: pd.DataFrame) -> TradeSignal:
        hold = TradeSignal(signal=Signal.HOLD, symbol=self.symbol, token=self.token)

        n = len(history)
        min_bars = max(self.orb_candles + 1, self.ema_slow + 5,
                       self.rsi_period + 2, self.vol_avg_periods + 1)
        if n < min_bars:
            return hold

        closes  = history["close"]
        highs   = history["high"]
        lows    = history["low"]
        volumes = history["volume"]

        # --- Indicators ---
        ema_f_series = ema(closes, self.ema_fast)
        ema_s_series = ema(closes, self.ema_slow)
        ema_f        = ema_f_series.iloc[-1]
        ema_s        = ema_s_series.iloc[-1]
        rsi_val      = rsi(closes, self.rsi_period).iloc[-1]
        atr_val      = atr(highs, lows, closes, self.atr_period).iloc[-1]
        vwap_v       = vwap(highs, lows, closes, volumes).iloc[-1]
        avg_vol      = float(volumes.iloc[-self.vol_avg_periods:].mean())
        cur_vol      = float(candle["volume"])

        # --- ORB (set once per session) ---
        if self._orb_high is None and n >= self.orb_candles:
            self._orb_high, self._orb_low = opening_range(history, self.orb_candles)
            logger.info(f"[{self.symbol}] ORB set: H={self._orb_high:.2f}, L={self._orb_low:.2f}")

        if self._orb_high is None or n <= self.orb_candles:
            return hold

        close = float(candle["close"])

        # === EXIT checks on existing position ===
        if self._position is not None:
            return self._check_exits(close, ema_f, ema_s)

        # === ENTRY checks ===
        vwap_ok_long  = (close > vwap_v) if self.vwap_filter else True
        vwap_ok_short = (close < vwap_v) if self.vwap_filter else True

        # --- LONG setup ---
        if (close > self._orb_high and ema_f > ema_s
                and rsi_val < self.rsi_overbought and vwap_ok_long):

            sl     = close - (atr_val * self.atr_stop_mult)
            target = close + (atr_val * self.atr_target_mult)
            score, factors = self._score_long(
                close, ema_f, ema_s, rsi_val, vwap_v, cur_vol, avg_vol, sl, target
            )

            if score < self.min_confidence:
                logger.debug(f"[{self.symbol}] LONG setup found but confidence too low: {score:.0f}")
                return hold

            self._position    = "long"
            self._entry_price = close
            self._stop_loss   = sl
            self._target      = target

            reason = self._build_reason("LONG", close, ema_f, ema_s, rsi_val, vwap_v,
                                         cur_vol, avg_vol, score)
            return TradeSignal(
                signal=Signal.BUY, symbol=self.symbol, token=self.token,
                entry_price=close, stop_loss=round(sl, 2), target=round(target, 2),
                atr=round(atr_val, 2), reason=reason,
                confidence=round(score, 1), confidence_factors=factors,
                entry_window_mins=self.candle_minutes,
            )

        # --- SHORT setup ---
        if (close < self._orb_low and ema_f < ema_s
                and rsi_val > self.rsi_oversold and vwap_ok_short):

            sl     = close + (atr_val * self.atr_stop_mult)
            target = close - (atr_val * self.atr_target_mult)
            score, factors = self._score_short(
                close, ema_f, ema_s, rsi_val, vwap_v, cur_vol, avg_vol, sl, target
            )

            if score < self.min_confidence:
                logger.debug(f"[{self.symbol}] SHORT setup found but confidence too low: {score:.0f}")
                return hold

            self._position    = "short"
            self._entry_price = close
            self._stop_loss   = sl
            self._target      = target

            reason = self._build_reason("SHORT", close, ema_f, ema_s, rsi_val, vwap_v,
                                         cur_vol, avg_vol, score)
            return TradeSignal(
                signal=Signal.SELL, symbol=self.symbol, token=self.token,
                entry_price=close, stop_loss=round(sl, 2), target=round(target, 2),
                atr=round(atr_val, 2), reason=reason,
                confidence=round(score, 1), confidence_factors=factors,
                entry_window_mins=self.candle_minutes,
            )

        return hold

    # ---------------------------------------------------------------
    # Exit logic
    # ---------------------------------------------------------------

    def _check_exits(self, close: float, ema_f: float, ema_s: float) -> TradeSignal:
        hold = TradeSignal(signal=Signal.HOLD, symbol=self.symbol, token=self.token)

        if self._position == "long":
            if close <= self._stop_loss:
                reason = f"Stop hit: ₹{close:.2f} ≤ SL ₹{self._stop_loss:.2f}"
                self._position = None
                return TradeSignal(signal=Signal.EXIT_LONG, symbol=self.symbol,
                                   token=self.token, entry_price=close, reason=reason)
            if close >= self._target:
                reason = f"Target hit: ₹{close:.2f} ≥ T ₹{self._target:.2f}"
                self._position = None
                return TradeSignal(signal=Signal.EXIT_LONG, symbol=self.symbol,
                                   token=self.token, entry_price=close, reason=reason)
            if ema_f < ema_s:
                reason = "EMA bearish crossover — exit long"
                self._position = None
                return TradeSignal(signal=Signal.EXIT_LONG, symbol=self.symbol,
                                   token=self.token, entry_price=close, reason=reason)

        elif self._position == "short":
            if close >= self._stop_loss:
                reason = f"Stop hit: ₹{close:.2f} ≥ SL ₹{self._stop_loss:.2f}"
                self._position = None
                return TradeSignal(signal=Signal.EXIT_SHORT, symbol=self.symbol,
                                   token=self.token, entry_price=close, reason=reason)
            if close <= self._target:
                reason = f"Target hit: ₹{close:.2f} ≤ T ₹{self._target:.2f}"
                self._position = None
                return TradeSignal(signal=Signal.EXIT_SHORT, symbol=self.symbol,
                                   token=self.token, entry_price=close, reason=reason)
            if ema_f > ema_s:
                reason = "EMA bullish crossover — exit short"
                self._position = None
                return TradeSignal(signal=Signal.EXIT_SHORT, symbol=self.symbol,
                                   token=self.token, entry_price=close, reason=reason)

        return hold

    # ---------------------------------------------------------------
    # Confidence scoring (0-100, sum of 6 factors × 0-20 each)
    # ---------------------------------------------------------------

    def _score_long(self, close, ema_f, ema_s, rsi_val, vwap_v,
                    cur_vol, avg_vol, sl, target) -> tuple[float, dict]:
        factors = {}

        # 1. ORB breakout strength (how far above ORB high, capped at 1 ATR)
        orb_pct = min((close - self._orb_high) / (self._orb_high + 1e-9) * 100, 2.0)
        factors["orb_breakout"] = round(min(20, orb_pct * 10), 1)

        # 2. EMA separation (trend strength)
        ema_sep_pct = (ema_f - ema_s) / (ema_s + 1e-9) * 100
        factors["ema_trend"] = round(min(20, ema_sep_pct * 50), 1)

        # 3. VWAP distance — ideal: just above (0.1-0.5%)
        vwap_dist_pct = (close - vwap_v) / (vwap_v + 1e-9) * 100
        if 0.1 <= vwap_dist_pct <= 1.0:
            factors["vwap_position"] = 20.0
        elif vwap_dist_pct > 1.0:
            factors["vwap_position"] = round(max(0, 20 - (vwap_dist_pct - 1.0) * 10), 1)
        else:
            factors["vwap_position"] = 0.0  # Below VWAP — no score for long

        # 4. RSI quality — ideal 50-60 for long entry
        if 50 <= rsi_val <= 60:
            factors["rsi_quality"] = 20.0
        elif 45 <= rsi_val < 50 or 60 < rsi_val <= 65:
            factors["rsi_quality"] = 15.0
        elif 40 <= rsi_val < 45 or 65 < rsi_val <= 70:
            factors["rsi_quality"] = 8.0
        else:
            factors["rsi_quality"] = 0.0

        # 5. Volume confirmation (current candle volume vs. average)
        vol_ratio = cur_vol / (avg_vol + 1e-9)
        if vol_ratio >= 1.5:
            factors["volume"] = 20.0
        elif vol_ratio >= 1.2:
            factors["volume"] = 15.0
        elif vol_ratio >= 1.0:
            factors["volume"] = 10.0
        else:
            factors["volume"] = 0.0  # Below-average volume = weak signal

        # 6. Risk:Reward ratio
        risk   = abs(close - sl)
        reward = abs(target - close)
        rr     = reward / risk if risk > 0 else 0
        if rr >= 2.5:
            factors["rr_ratio"] = 20.0
        elif rr >= 2.0:
            factors["rr_ratio"] = 15.0
        elif rr >= 1.5:
            factors["rr_ratio"] = 10.0
        else:
            factors["rr_ratio"] = 0.0  # Bad R:R — never alert

        total = sum(factors.values())
        return total, factors

    def _score_short(self, close, ema_f, ema_s, rsi_val, vwap_v,
                     cur_vol, avg_vol, sl, target) -> tuple[float, dict]:
        factors = {}

        # 1. ORB breakdown strength
        orb_pct = min((self._orb_low - close) / (self._orb_low + 1e-9) * 100, 2.0)
        factors["orb_breakout"] = round(min(20, orb_pct * 10), 1)

        # 2. EMA separation
        ema_sep_pct = (ema_s - ema_f) / (ema_s + 1e-9) * 100
        factors["ema_trend"] = round(min(20, ema_sep_pct * 50), 1)

        # 3. VWAP — ideal: just below VWAP (0.1-0.5%)
        vwap_dist_pct = (vwap_v - close) / (vwap_v + 1e-9) * 100
        if 0.1 <= vwap_dist_pct <= 1.0:
            factors["vwap_position"] = 20.0
        elif vwap_dist_pct > 1.0:
            factors["vwap_position"] = round(max(0, 20 - (vwap_dist_pct - 1.0) * 10), 1)
        else:
            factors["vwap_position"] = 0.0

        # 4. RSI quality — ideal 40-50 for short
        if 40 <= rsi_val <= 50:
            factors["rsi_quality"] = 20.0
        elif 35 <= rsi_val < 40 or 50 < rsi_val <= 55:
            factors["rsi_quality"] = 15.0
        elif 30 <= rsi_val < 35 or 55 < rsi_val <= 60:
            factors["rsi_quality"] = 8.0
        else:
            factors["rsi_quality"] = 0.0

        # 5. Volume
        vol_ratio = cur_vol / (avg_vol + 1e-9)
        if vol_ratio >= 1.5:
            factors["volume"] = 20.0
        elif vol_ratio >= 1.2:
            factors["volume"] = 15.0
        elif vol_ratio >= 1.0:
            factors["volume"] = 10.0
        else:
            factors["volume"] = 0.0

        # 6. R:R
        risk   = abs(sl - close)
        reward = abs(close - target)
        rr     = reward / risk if risk > 0 else 0
        if rr >= 2.5:
            factors["rr_ratio"] = 20.0
        elif rr >= 2.0:
            factors["rr_ratio"] = 15.0
        elif rr >= 1.5:
            factors["rr_ratio"] = 10.0
        else:
            factors["rr_ratio"] = 0.0

        total = sum(factors.values())
        return total, factors

    # ---------------------------------------------------------------
    # Reason string builder
    # ---------------------------------------------------------------

    def _build_reason(self, direction, close, ema_f, ema_s, rsi_val,
                       vwap_v, cur_vol, avg_vol, score) -> str:
        orb_ref = self._orb_high if direction == "LONG" else self._orb_low
        return (
            f"{direction} | Score={score:.0f}/100 | "
            f"Close=₹{close:.2f} {'>' if direction=='LONG' else '<'} ORB={'H' if direction=='LONG' else 'L'}=₹{orb_ref:.2f} | "
            f"EMA9={ema_f:.2f} EMA21={ema_s:.2f} | "
            f"RSI={rsi_val:.1f} | VWAP=₹{vwap_v:.2f} | "
            f"Vol={cur_vol:,.0f} (avg {avg_vol:,.0f})"
        )


# ---------------------------------------------------------------
# PLACEHOLDER: Automatic Order Execution Hook
# ---------------------------------------------------------------
# This function is intentionally left as a stub.
# When you're ready to enable auto-execution, implement this
# and call it from main.py after a TradeSignal is received.
#
# def _execute_placeholder(trade_signal: TradeSignal, order_manager, position_sizer,
#                           circuit_breaker, position_manager) -> str | None:
#     """
#     Auto-execute a trade signal.
#     CURRENTLY DISABLED — notifications only mode.
#
#     Steps when re-enabled:
#       1. breaker.can_trade()       → check risk gate
#       2. sizer.compute_qty()       → size the position
#       3. order_manager.place_order() → submit to Angel One
#       4. tracker.watch(order_id)   → reconcile fill
#       5. position_manager.open_position() → track state
#     """
#     raise NotImplementedError("Auto-execution not yet enabled. System is in notification-only mode.")
