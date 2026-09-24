"""
strategy/signal_engine.py

ORB + EMA crossover + VWAP filter + RSI filter + ATR stops
WITH CONFIDENCE SCORING (0-100).

Confidence is built from 5 independent factors, each scored 0-20. This is a
HYBRID of two backtested designs (see tools/analyze_confidence_factors.py +
backtest_results_baseline90 vs backtest_results_baseline90_redesign): per-factor
correlation with win/pnl was compared for the raw-%-distance formulas vs. an
ATR-normalized sweet-spot-band redesign, and each factor kept whichever version
scored better (neither design was a clean sweep):
  1. ORB breakout strength   — raw %-distance past ORB high/low (old formula;
                                marginally better win-correlation than the ATR version)
  2. EMA alignment           — raw %-separation of EMA9/21 (old formula; ATR
                                version saturated 74% of trades into one bucket)
  3. VWAP position           — price distance from VWAP in ATR units (redesigned;
                                flipped a wrong-signed correlation to correct-signed)
  4. RSI quality             — RSI in ideal entry zone (unchanged either way —
                                this is the one factor with genuine signal)
  5. ADX trend strength      — per-symbol ADX regime strength (redesigned;
                                replaces the old wrong-signed `volume` factor)

Only signals with confidence >= MIN_CONFIDENCE_THRESHOLD are emitted as BUY/SELL.
Below threshold: HOLD is returned (not worth alerting).

Design: automatic order execution is a PLACEHOLDER — see _execute_placeholder().
"""

from __future__ import annotations
import logging
from datetime import datetime

import numpy as np
import pandas as pd

from .strategy_base import StrategyBase, TradeSignal, Signal
from .indicators    import ema, rsi, atr, vwap, opening_range, adx

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
        breakeven_r          : float = 1.0    (move stop to breakeven after price moves this many R)
        trail_atr_mult       : float = 1.0    (ATR multiple used to trail stop once breakeven is hit)
        max_holding_candles  : int   = 0      (force exit after N candles in position; 0 = disabled)
        early_cut_candles    : int   = 0      (cut a stalled trade after N candles if still below early_cut_min_r; 0 = disabled)
        early_cut_min_r      : float = 0.3    (R-multiple threshold below which a trade is considered "stalled")
        min_atr_pct          : float = 0.0    (skip entries if ATR/close % is below this — too illiquid/choppy)
        max_atr_pct          : float = 100.0  (skip entries if ATR/close % is above this — too volatile/news-risk)
        adx_period           : int   = 14     (period for the ADX regime filter)
        min_adx              : float = 0.0    (skip entries if ADX is below this — choppy/non-trending regime; 0 = disabled)
        min_ema_trend_factor : float = 0.0    (skip entries if the ema_trend confidence sub-factor is below this, out of 20; 0 = disabled)
        confirmation_candles : int   = 0      (require this many consecutive same-direction candles — including the signal candle — before entering; 0 = disabled)
        min_entry_time       : str   = None   (skip new entries before this clock time, e.g. "11:00"; None/empty = disabled, current live behavior)
        early_session_end_time      : str   = None (widen the stop-loss for entries before this clock time; None/empty = disabled)
        early_session_atr_stop_mult : float = None (ATR stop multiplier to use for entries before early_session_end_time, e.g. 2.0; ignored unless early_session_end_time is set)
        require_orb_retest   : bool  = False  (instead of entering on the raw breakout candle, wait for price to pull back near the ORB level and resume in the breakout direction; False = disabled, current live behavior)
        retest_tolerance_atr : float = 0.15   (how close, in ATR multiples, price must pull back to the ORB level to count as a retest)
        retest_timeout_candles : int = 6      (cancel a pending breakout if no confirmed retest+resume happens within this many candles)
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
        self.breakeven_r       = float(p.get("breakeven_r",        1.0))
        self.trail_atr_mult    = float(p.get("trail_atr_mult",     1.0))
        self.max_holding_candles = int(p.get("max_holding_candles", 0))
        self.early_cut_candles = int(p.get("early_cut_candles", 0))    # 0 = disabled
        self.early_cut_min_r   = float(p.get("early_cut_min_r",  0.3))
        self.min_atr_pct       = float(p.get("min_atr_pct",        0.0))
        self.max_atr_pct       = float(p.get("max_atr_pct",      100.0))
        self.adx_period        = int(p.get("adx_period",           14))
        self.min_adx           = float(p.get("min_adx",           0.0))
        self.min_ema_trend_factor = float(p.get("min_ema_trend_factor", 0.0))
        self.confirmation_candles = int(p.get("confirmation_candles", 0))
        min_entry_time_str     = p.get("min_entry_time") or None
        self.min_entry_time     = (datetime.strptime(min_entry_time_str, "%H:%M").time()
                                    if min_entry_time_str else None)
        early_session_end_str   = p.get("early_session_end_time") or None
        self.early_session_end_time = (datetime.strptime(early_session_end_str, "%H:%M").time()
                                        if early_session_end_str else None)
        early_session_mult       = p.get("early_session_atr_stop_mult")
        self.early_session_atr_stop_mult = float(early_session_mult) if early_session_mult is not None else None
        self.require_orb_retest     = bool(p.get("require_orb_retest", False))
        self.retest_tolerance_atr   = float(p.get("retest_tolerance_atr", 0.15))
        self.retest_timeout_candles = int(p.get("retest_timeout_candles", 6))

        # Session state
        self._position         = None     # None | "long" | "short"
        self._entry_price      = 0.0
        self._stop_loss        = 0.0
        self._target           = 0.0
        self._orb_high         = None
        self._orb_low          = None
        self._initial_risk     = 0.0      # |entry - initial stop|, used for breakeven trigger
        self._breakeven_done   = False
        self._candles_held     = 0
        self._pending_breakout  = None     # None | "long" | "short" — awaiting ORB retest
        self._pending_candles   = 0
        self._retest_seen       = False

    def reset(self) -> None:
        self._position   = None
        self._entry_price = 0.0
        self._stop_loss   = 0.0
        self._target      = 0.0
        self._orb_high    = None
        self._orb_low     = None
        self._initial_risk   = 0.0
        self._breakeven_done = False
        self._candles_held   = 0
        self._pending_breakout = None
        self._pending_candles  = 0
        self._retest_seen      = False

    def force_exit(self) -> None:
        """Sync internal state after an external hard close (SL/target/EOD hit by caller)."""
        self._position       = None
        self._entry_price    = 0.0
        self._stop_loss      = 0.0
        self._target         = 0.0
        self._initial_risk   = 0.0
        self._breakeven_done = False
        self._candles_held   = 0

    def get_current_stop(self) -> float | None:
        """Live stop-loss (post breakeven/trailing), for callers tracking an intrabar hard stop."""
        return self._stop_loss if self._position is not None else None

    def get_current_target(self) -> float | None:
        """Live target, for callers tracking an intrabar hard target."""
        return self._target if self._position is not None else None

    def get_position_direction(self) -> str | None:
        """'long' | 'short' | None — for callers tracking intrabar exits."""
        return self._position

    def get_entry_price(self) -> float | None:
        return self._entry_price if self._position is not None else None

    # ---------------------------------------------------------------
    # Main candle handler
    # ---------------------------------------------------------------

    def on_candle_close(self, candle: pd.Series, history: pd.DataFrame) -> TradeSignal:
        hold = TradeSignal(signal=Signal.HOLD, symbol=self.symbol, token=self.token)

        n = len(history)
        min_bars = max(self.orb_candles + 1, self.ema_slow + 5,
                       self.rsi_period + 2, self.vol_avg_periods + 1)
        if self.min_adx > 0:
            min_bars = max(min_bars, self.adx_period + 2)
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
        adx_val      = adx(highs, lows, closes, self.adx_period).iloc[-1]
        avg_vol      = float(volumes.iloc[-self.vol_avg_periods:].mean())
        cur_vol      = float(candle["volume"])

        # --- ORB (set once per session) ---
        # NOTE: `history` is the FULL cumulative multi-day history (warm-up +
        # every candle since), not just today's bars — opening_range() must be
        # restricted to today's rows only, or it silently computes the range
        # of the very first candles ever seen (e.g. ~30 days ago), not today's
        # actual opening range. This was a real bug: `n`/`history` here used
        # to be the whole dataset, so every day after the first one in a given
        # run computed ORB off a stale, unrelated reference level.
        if "timestamp" in history.columns:
            history = history.copy()
            history["timestamp"] = pd.to_datetime(history["timestamp"], errors="coerce")
            candle_ts = pd.to_datetime(candle.get("timestamp"), errors="coerce")
            if pd.isna(candle_ts):
                return hold
            cur_date_orb = candle_ts.date()
            today_hist = history[history["timestamp"].dt.date == cur_date_orb]
        else:
            today_hist = history
            candle_ts  = None
        n_today = len(today_hist)

        if self._orb_high is None and n_today >= self.orb_candles:
            self._orb_high, self._orb_low = opening_range(today_hist, self.orb_candles)
            logger.info(f"[{self.symbol}] ORB set: H={self._orb_high:.2f}, L={self._orb_low:.2f}")

        if self._orb_high is None or n_today <= self.orb_candles:
            return hold

        close = float(candle["close"])

        # === EXIT checks on existing position ===
        if self._position is not None:
            return self._check_exits(close, ema_f, ema_s, atr_val)

        # === Entry-time gate — skip new entries before a configured clock time
        # (e.g. the noisy 9:30-10:59 post-ORB window is disproportionately whipsaw-prone)
        if self.min_entry_time is not None and candle_ts is not None and candle_ts.time() < self.min_entry_time:
            return hold

        # === Volatility gate — skip entries on too-illiquid/choppy or too-wild stocks ===
        atr_pct = (atr_val / close * 100) if close else 0.0
        if not (self.min_atr_pct <= atr_pct <= self.max_atr_pct):
            return hold

        # === Regime gate — skip entries when ADX shows a non-trending/choppy market ===
        if self.min_adx > 0 and (pd.isna(adx_val) or adx_val < self.min_adx):
            return hold

        # === Candle-confirmation gate — require N consecutive same-direction candles
        # (including the signal candle) before entering, instead of firing on a single
        # candle touching the breakout level. Cuts down single-candle whipsaw entries.
        opens = history["open"]
        bullish_confirmed = True
        bearish_confirmed = True
        if self.confirmation_candles > 0:
            recent_closes = closes.iloc[-self.confirmation_candles:]
            recent_opens  = opens.iloc[-self.confirmation_candles:]
            bullish_confirmed = bool((recent_closes.values > recent_opens.values).all())
            bearish_confirmed = bool((recent_closes.values < recent_opens.values).all())

        # === ENTRY checks ===
        vwap_ok_long  = (close > vwap_v) if self.vwap_filter else True
        vwap_ok_short = (close < vwap_v) if self.vwap_filter else True

        raw_long_breakout  = (close > self._orb_high and ema_f > ema_s
                               and rsi_val < self.rsi_overbought and vwap_ok_long and bullish_confirmed)
        raw_short_breakout = (close < self._orb_low and ema_f < ema_s
                               and rsi_val > self.rsi_oversold and vwap_ok_short and bearish_confirmed)

        if self.require_orb_retest:
            low  = float(candle["low"])
            high = float(candle["high"])
            long_entry_ready, short_entry_ready = self._update_orb_retest_state(
                close, low, high, atr_val, raw_long_breakout, raw_short_breakout
            )
        else:
            long_entry_ready, short_entry_ready = raw_long_breakout, raw_short_breakout

        # Early-session stop widening — the 9:30-10:59 post-ORB window is
        # disproportionately whipsaw-prone; a wider stop gives entries in this
        # window more room before getting stopped out by noise.
        stop_mult = self.atr_stop_mult
        if (self.early_session_end_time is not None and self.early_session_atr_stop_mult is not None
                and candle_ts is not None and candle_ts.time() < self.early_session_end_time):
            stop_mult = self.early_session_atr_stop_mult

        # --- LONG setup ---
        if long_entry_ready:

            sl     = close - (atr_val * stop_mult)
            target = close + (atr_val * self.atr_target_mult)
            score, factors = self._score_long(
                close, ema_f, ema_s, rsi_val, vwap_v, atr_val, adx_val
            )

            if score < self.min_confidence:
                logger.debug(f"[{self.symbol}] LONG setup found but confidence too low: {score:.0f}")
                return hold

            if factors["ema_trend"] < self.min_ema_trend_factor:
                logger.debug(f"[{self.symbol}] LONG setup found but trend too weak: "
                             f"ema_trend={factors['ema_trend']:.1f}")
                return hold

            self._position       = "long"
            self._entry_price    = close
            self._stop_loss      = sl
            self._target         = target
            self._initial_risk   = abs(close - sl)
            self._breakeven_done = False
            self._candles_held   = 0

            reason = self._build_reason("LONG", close, ema_f, ema_s, rsi_val, vwap_v,
                                         cur_vol, avg_vol, adx_val, score)
            return TradeSignal(
                signal=Signal.BUY, symbol=self.symbol, token=self.token,
                entry_price=close, stop_loss=round(sl, 2), target=round(target, 2),
                atr=round(atr_val, 2), reason=reason,
                confidence=round(score, 1), confidence_factors=factors,
                entry_window_mins=self.candle_minutes,
            )

        # --- SHORT setup ---
        if short_entry_ready:

            sl     = close + (atr_val * stop_mult)
            target = close - (atr_val * self.atr_target_mult)
            score, factors = self._score_short(
                close, ema_f, ema_s, rsi_val, vwap_v, atr_val, adx_val
            )

            if score < self.min_confidence:
                logger.debug(f"[{self.symbol}] SHORT setup found but confidence too low: {score:.0f}")
                return hold

            if factors["ema_trend"] < self.min_ema_trend_factor:
                logger.debug(f"[{self.symbol}] SHORT setup found but trend too weak: "
                             f"ema_trend={factors['ema_trend']:.1f}")
                return hold

            self._position       = "short"
            self._entry_price    = close
            self._stop_loss      = sl
            self._target         = target
            self._initial_risk   = abs(sl - close)
            self._breakeven_done = False
            self._candles_held   = 0

            reason = self._build_reason("SHORT", close, ema_f, ema_s, rsi_val, vwap_v,
                                         cur_vol, avg_vol, adx_val, score)
            return TradeSignal(
                signal=Signal.SELL, symbol=self.symbol, token=self.token,
                entry_price=close, stop_loss=round(sl, 2), target=round(target, 2),
                atr=round(atr_val, 2), reason=reason,
                confidence=round(score, 1), confidence_factors=factors,
                entry_window_mins=self.candle_minutes,
            )

        return hold

    # ---------------------------------------------------------------
    # ORB-retest state machine (opt-in via require_orb_retest)
    # ---------------------------------------------------------------

    def _update_orb_retest_state(self, close: float, low: float, high: float, atr_val: float,
                                  raw_long_breakout: bool, raw_short_breakout: bool) -> tuple[bool, bool]:
        """
        Instead of entering on the raw breakout candle, require price to pull back
        (retest) close to the ORB level and then resume in the breakout direction
        before actually entering — filters out one-candle fakeouts that never
        confirm. Cancels the pending setup if price reverses hard through the
        opposite ORB level, or if no retest+resume happens within
        retest_timeout_candles.
        """
        tol = self.retest_tolerance_atr * atr_val

        if self._pending_breakout == "long":
            self._pending_candles += 1
            if close < self._orb_low or self._pending_candles > self.retest_timeout_candles:
                self._pending_breakout = None
                return False, False
            if low <= self._orb_high + tol:
                self._retest_seen = True
            if self._retest_seen and raw_long_breakout:
                self._pending_breakout = None
                return True, False
            return False, False

        if self._pending_breakout == "short":
            self._pending_candles += 1
            if close > self._orb_high or self._pending_candles > self.retest_timeout_candles:
                self._pending_breakout = None
                return False, False
            if high >= self._orb_low - tol:
                self._retest_seen = True
            if self._retest_seen and raw_short_breakout:
                self._pending_breakout = None
                return False, True
            return False, False

        # No pending breakout yet — arm on a fresh raw breakout, but don't enter
        # immediately; wait for the retest+resume on a later candle.
        if raw_long_breakout:
            self._pending_breakout = "long"
            self._pending_candles  = 0
            self._retest_seen      = False
        elif raw_short_breakout:
            self._pending_breakout = "short"
            self._pending_candles  = 0
            self._retest_seen      = False
        return False, False

    # ---------------------------------------------------------------
    # Exit logic
    # ---------------------------------------------------------------

    def _check_exits(self, close: float, ema_f: float, ema_s: float, atr_val: float) -> TradeSignal:
        hold = TradeSignal(signal=Signal.HOLD, symbol=self.symbol, token=self.token)
        self._candles_held += 1

        if self._position == "long":
            # Breakeven: once price has moved breakeven_r * initial_risk in our favor,
            # lock the stop at (or above) entry so a reversal can no longer produce a loss.
            if not self._breakeven_done and self._initial_risk > 0:
                if (close - self._entry_price) >= self.breakeven_r * self._initial_risk:
                    self._stop_loss = max(self._stop_loss, self._entry_price)
                    self._breakeven_done = True
                    logger.info(f"[{self.symbol}] Long stop moved to breakeven ₹{self._stop_loss:.2f}")

            # Cascading early exit: if the trade is still stalled (below early_cut_min_r)
            # after early_cut_candles, hard-exit rather than let it bleed further.
            # (A conditional-tighten-stop variant was tested and found to be a net
            # regression vs. this plain hard-cut across two backtest sweeps — removed.)
            if (self.early_cut_candles and not self._breakeven_done
                    and self._candles_held >= self.early_cut_candles and self._initial_risk > 0):
                current_r = (close - self._entry_price) / self._initial_risk
                if current_r < self.early_cut_min_r:
                    reason = f"Stalled ({self._candles_held} candles, {current_r:.2f}R) — cut early"
                    self._position = None
                    return TradeSignal(signal=Signal.EXIT_LONG, symbol=self.symbol,
                                       token=self.token, entry_price=self._entry_price, reason=reason)
            # ATR trailing stop — only ratchets up, never loosens
            if self._breakeven_done:
                trail_sl = close - atr_val * self.trail_atr_mult
                if trail_sl > self._stop_loss:
                    self._stop_loss = trail_sl

            if close <= self._stop_loss:
                reason = f"Stop hit: ₹{close:.2f} ≤ SL ₹{self._stop_loss:.2f}"
                self._position = None
                return TradeSignal(signal=Signal.EXIT_LONG, symbol=self.symbol,
                                   token=self.token, entry_price=self._entry_price, reason=reason)
            if close >= self._target:
                reason = f"Target hit: ₹{close:.2f} ≥ T ₹{self._target:.2f}"
                self._position = None
                return TradeSignal(signal=Signal.EXIT_LONG, symbol=self.symbol,
                                   token=self.token, entry_price=self._entry_price, reason=reason)
            if self.max_holding_candles and self._candles_held >= self.max_holding_candles:
                reason = f"Max holding period ({self.max_holding_candles} candles) reached — exit long"
                self._position = None
                return TradeSignal(signal=Signal.EXIT_LONG, symbol=self.symbol,
                                   token=self.token, entry_price=self._entry_price, reason=reason)
            if ema_f < ema_s:
                reason = "EMA bearish crossover — exit long"
                self._position = None
                return TradeSignal(signal=Signal.EXIT_LONG, symbol=self.symbol,
                                   token=self.token, entry_price=self._entry_price, reason=reason)

        elif self._position == "short":
            if not self._breakeven_done and self._initial_risk > 0:
                if (self._entry_price - close) >= self.breakeven_r * self._initial_risk:
                    self._stop_loss = min(self._stop_loss, self._entry_price)
                    self._breakeven_done = True
                    logger.info(f"[{self.symbol}] Short stop moved to breakeven ₹{self._stop_loss:.2f}")

            if (self.early_cut_candles and not self._breakeven_done
                    and self._candles_held >= self.early_cut_candles and self._initial_risk > 0):
                current_r = (self._entry_price - close) / self._initial_risk
                if current_r < self.early_cut_min_r:
                    reason = f"Stalled ({self._candles_held} candles, {current_r:.2f}R) — cut early"
                    self._position = None
                    return TradeSignal(signal=Signal.EXIT_SHORT, symbol=self.symbol,
                                       token=self.token, entry_price=self._entry_price, reason=reason)
            if self._breakeven_done:
                trail_sl = close + atr_val * self.trail_atr_mult
                if trail_sl < self._stop_loss:
                    self._stop_loss = trail_sl

            if close >= self._stop_loss:
                reason = f"Stop hit: ₹{close:.2f} ≥ SL ₹{self._stop_loss:.2f}"
                self._position = None
                return TradeSignal(signal=Signal.EXIT_SHORT, symbol=self.symbol,
                                   token=self.token, entry_price=self._entry_price, reason=reason)
            if close <= self._target:
                reason = f"Target hit: ₹{close:.2f} ≤ T ₹{self._target:.2f}"
                self._position = None
                return TradeSignal(signal=Signal.EXIT_SHORT, symbol=self.symbol,
                                   token=self.token, entry_price=self._entry_price, reason=reason)
            if self.max_holding_candles and self._candles_held >= self.max_holding_candles:
                reason = f"Max holding period ({self.max_holding_candles} candles) reached — exit short"
                self._position = None
                return TradeSignal(signal=Signal.EXIT_SHORT, symbol=self.symbol,
                                   token=self.token, entry_price=self._entry_price, reason=reason)
            if ema_f > ema_s:
                reason = "EMA bullish crossover — exit short"
                self._position = None
                return TradeSignal(signal=Signal.EXIT_SHORT, symbol=self.symbol,
                                   token=self.token, entry_price=self._entry_price, reason=reason)

        return hold

    # ---------------------------------------------------------------
    # Confidence scoring (0-100, sum of 5 factors x 0-20 each). HYBRID: orb_breakout
    # and ema_trend use the old raw-%-distance formulas (backtested as marginally
    # better/no-worse than the ATR-normalized redesign); vwap_position and
    # adx_trend use the ATR-normalized/regime-based redesign (backtested as a
    # genuine improvement over the old vwap %-distance and `volume` formulas).
    # See tools/analyze_confidence_factors.py + backtest_results_baseline90 vs.
    # backtest_results_baseline90_redesign for the comparison that drove this.
    # ---------------------------------------------------------------

    @staticmethod
    def _sweet_spot_score(x: float, lo: float, hi: float, ramp_in: float,
                          decay_span: float, floor: float = 0.0) -> float:
        """Score that ramps 0->20 up to [lo,hi], holds 20 inside the band, then
        decays down to `floor` over `decay_span` beyond `hi`. `ramp_in` sets how
        quickly sub-`lo` values ramp up (0 at x=0)."""
        if x <= 0:
            return 0.0
        if x < lo:
            return round(min(20.0, x / max(ramp_in, 1e-9) * 20.0), 1)
        if x <= hi:
            return 20.0
        return round(max(floor, 20.0 - (x - hi) / max(decay_span, 1e-9) * (20.0 - floor)), 1)

    def _score_long(self, close, ema_f, ema_s, rsi_val, vwap_v,
                    atr_val, adx_val) -> tuple[float, dict]:
        factors = {}
        atr_safe = atr_val + 1e-9

        # 1. ORB breakout strength — reverted to old raw-%-distance formula:
        # backtest correlation analysis showed this scored marginally better
        # (corr(win)=+0.031) than the ATR-normalized sweet-spot version
        # (corr(win)=+0.005); the ATR version isn't a clear win here.
        orb_pct = min((close - self._orb_high) / (self._orb_high + 1e-9) * 100, 2.0)
        factors["orb_breakout"] = round(min(20, orb_pct * 10), 1)

        # 2. EMA separation — reverted to old raw-%-distance formula: the ATR
        # sweet-spot version saturated 74% of trades into its top bucket
        # (worse resolution) with no correlation improvement to compensate.
        ema_sep_pct = (ema_f - ema_s) / (ema_s + 1e-9) * 100
        factors["ema_trend"] = round(min(20, ema_sep_pct * 50), 1)

        # 3. VWAP distance in ATR units — KEPT (redesigned): flipped the
        # correlation sign to the correct direction vs. the old %-distance
        # formula (corr(win) -0.033 -> +0.020), a genuine improvement.
        vwap_dist_atr = (close - vwap_v) / atr_safe
        factors["vwap_position"] = self._sweet_spot_score(vwap_dist_atr, 0.05, 0.3, 0.05, 0.7, floor=0.0)

        # 4. RSI quality — ideal 50-60 for long entry
        if 50 <= rsi_val <= 60:
            factors["rsi_quality"] = 20.0
        elif 45 <= rsi_val < 50 or 60 < rsi_val <= 65:
            factors["rsi_quality"] = 15.0
        elif 40 <= rsi_val < 45 or 65 < rsi_val <= 70:
            factors["rsi_quality"] = 8.0
        else:
            factors["rsi_quality"] = 0.0

        # 5. ADX trend-strength regime (direction-agnostic — same for long/short).
        factors["adx_trend"] = self._adx_score(adx_val)

        total = min(100.0, sum(factors.values()))
        return total, factors

    def _score_short(self, close, ema_f, ema_s, rsi_val, vwap_v,
                     atr_val, adx_val) -> tuple[float, dict]:
        factors = {}
        atr_safe = atr_val + 1e-9

        # 1. ORB breakdown strength (mirror of _score_long) — old raw-% formula.
        orb_pct = min((self._orb_low - close) / (self._orb_low + 1e-9) * 100, 2.0)
        factors["orb_breakout"] = round(min(20, orb_pct * 10), 1)

        # 2. EMA separation — old raw-% formula.
        ema_sep_pct = (ema_s - ema_f) / (ema_s + 1e-9) * 100
        factors["ema_trend"] = round(min(20, ema_sep_pct * 50), 1)

        # 3. VWAP — ideal: just below VWAP, in ATR units (kept/redesigned).
        vwap_dist_atr = (vwap_v - close) / atr_safe
        factors["vwap_position"] = self._sweet_spot_score(vwap_dist_atr, 0.05, 0.3, 0.05, 0.7, floor=0.0)

        # 4. RSI quality — ideal 40-50 for short
        if 40 <= rsi_val <= 50:
            factors["rsi_quality"] = 20.0
        elif 35 <= rsi_val < 40 or 50 < rsi_val <= 55:
            factors["rsi_quality"] = 15.0
        elif 30 <= rsi_val < 35 or 55 < rsi_val <= 60:
            factors["rsi_quality"] = 8.0
        else:
            factors["rsi_quality"] = 0.0

        # 5. ADX trend-strength regime
        factors["adx_trend"] = self._adx_score(adx_val)

        total = min(100.0, sum(factors.values()))
        return total, factors

    @staticmethod
    def _adx_score(adx_val: float) -> float:
        """Conventional ADX thresholds: <15 no trend, 15-20 weak, 20-25 developing,
        25-35 strong, >=35 very strong. Direction-agnostic (ADX measures strength,
        not direction — the entry gates already establish direction)."""
        if pd.isna(adx_val):
            return 0.0
        if adx_val >= 35:
            return 20.0
        if adx_val >= 25:
            return 16.0
        if adx_val >= 20:
            return 10.0
        if adx_val >= 15:
            return 5.0
        return 0.0

    # ---------------------------------------------------------------
    # Reason string builder
    # ---------------------------------------------------------------

    def _build_reason(self, direction, close, ema_f, ema_s, rsi_val,
                       vwap_v, cur_vol, avg_vol, adx_val, score) -> str:
        orb_ref = self._orb_high if direction == "LONG" else self._orb_low
        adx_str = f"{adx_val:.1f}" if pd.notna(adx_val) else "n/a"
        return (
            f"{direction} | Score={score:.0f}/100 | "
            f"Close=₹{close:.2f} {'>' if direction=='LONG' else '<'} ORB={'H' if direction=='LONG' else 'L'}=₹{orb_ref:.2f} | "
            f"EMA9={ema_f:.2f} EMA21={ema_s:.2f} | "
            f"RSI={rsi_val:.1f} | VWAP=₹{vwap_v:.2f} | ADX={adx_str} | "
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
