"""
strategy/research_intraday.py

Two research-backed INTRADAY strategies (both square off same day), implemented
on the shared StrategyBase interface so they run in the existing portfolio
backtester (intraday mode) and, if validated, the live loop.

1) IntradayMomentumStrategy
   Based on the documented "market intraday momentum" effect (Gao, Han, Li &
   Zhou 2018, and multi-market replications): the first-half-hour return
   predicts the last-half-hour return. Implementation: read the first-30-min
   move (09:15->09:45, i.e. the first two 15-min bars), and at a configured
   late-session entry time take a position into the close IN THE SAME DIRECTION,
   held until the backtester's EOD square-off.

2) GapFadeStrategy
   Based on the documented gap-reversion effect: stocks that gap abnormally at
   the open tend to revert the gap during the session. Implementation: at the
   first bar, measure the overnight gap vs the prior day's close; if it exceeds
   a threshold, fade it (short a gap-up / long a gap-down), targeting a partial
   gap fill, with a stop beyond the open extreme, exit by EOD.

Both keep one trade per symbol per day and rely on the intraday-mode backtester
for the EOD square-off (so no fixed far target is needed for the momentum one).
"""

from __future__ import annotations
import logging
from datetime import datetime

import pandas as pd

from .strategy_base import StrategyBase, TradeSignal, Signal
from .indicators import atr

logger = logging.getLogger(__name__)


def _today(history: pd.DataFrame, candle):
    """Return (today_rows, candle_timestamp) with a datetime timestamp column."""
    if "timestamp" not in history.columns:
        return history, None
    h = history.copy()
    h["timestamp"] = pd.to_datetime(h["timestamp"], errors="coerce")
    cts = pd.to_datetime(candle.get("timestamp"), errors="coerce")
    if pd.isna(cts):
        return None, None
    return h[h["timestamp"].dt.date == cts.date()], cts


class IntradayMomentumStrategy(StrategyBase):
    """
    Config:
        first_window_bars : int   = 2      (# opening 15-min bars = first 30 min)
        entry_time        : str   = "14:00" (take the into-close position at/after this)
        min_move_pct      : float = 0.3    (only trade if |first-30-min move| >= this %)
        atr_period        : int   = 14
        atr_stop_mult     : float = 1.5    (protective stop; 0 = none, rely on EOD)
        min_price         : float = 50.0
    Exit: EOD square-off (backtester intraday mode) or protective ATR stop.
    """

    def __init__(self, symbol: str, token: str, **kwargs) -> None:
        super().__init__(symbol, token, **kwargs)
        p = kwargs
        self.first_window_bars = int(p.get("first_window_bars", 2))
        self.min_move_pct = float(p.get("min_move_pct", 0.3))
        self.atr_period = int(p.get("atr_period", 14))
        self.atr_stop_mult = float(p.get("atr_stop_mult", 1.5))
        self.min_price = float(p.get("min_price", 50.0))
        et = p.get("entry_time") or "14:00"
        self.entry_time = datetime.strptime(et, "%H:%M").time()

        self._position = None
        self._entry_price = 0.0
        self._stop_loss = 0.0
        self._entered_today = False

    def reset(self) -> None:
        self._position = None
        self._entry_price = 0.0
        self._stop_loss = 0.0
        self._entered_today = False

    def force_exit(self) -> None:
        self._position = None
        self._entry_price = 0.0
        self._stop_loss = 0.0

    def discard_pending_entry(self) -> None:
        self.force_exit()

    def get_current_stop(self):
        return self._stop_loss if (self._position is not None and self.atr_stop_mult) else None

    def get_current_target(self):
        return None

    def get_position_direction(self):
        return self._position

    def get_entry_price(self):
        return self._entry_price if self._position is not None else None

    def set_sector_momentum(self, pct: float) -> None:
        pass

    def on_candle_close(self, candle, history) -> TradeSignal:
        hold = TradeSignal(signal=Signal.HOLD, symbol=self.symbol, token=self.token)
        if self._position is not None:
            # only a protective stop; EOD square-off is handled by the backtester
            if self.atr_stop_mult:
                close = float(candle["close"])
                if self._position == "long" and close <= self._stop_loss:
                    d = self._position; self._position = None
                    return TradeSignal(signal=Signal.EXIT_LONG, symbol=self.symbol, token=self.token,
                                       entry_price=self._entry_price, reason="Protective stop")
                if self._position == "short" and close >= self._stop_loss:
                    self._position = None
                    return TradeSignal(signal=Signal.EXIT_SHORT, symbol=self.symbol, token=self.token,
                                       entry_price=self._entry_price, reason="Protective stop")
            return hold

        if self._entered_today:
            return hold
        today, cts = _today(history, candle)
        if today is None or cts is None or len(today) <= self.first_window_bars:
            return hold
        if cts.time() < self.entry_time:
            return hold

        first_open = float(today["open"].iloc[0])
        first_win_close = float(today["close"].iloc[self.first_window_bars - 1])
        if first_open <= 0:
            return hold
        move_pct = (first_win_close - first_open) / first_open * 100
        close = float(candle["close"])
        if close < self.min_price or abs(move_pct) < self.min_move_pct:
            self._entered_today = True   # decided not to trade today
            return hold

        atr_v = float(atr(history["high"], history["low"], history["close"], self.atr_period).iloc[-1])
        self._entered_today = True
        if move_pct > 0:
            self._position, self._entry_price = "long", close
            self._stop_loss = close - self.atr_stop_mult * atr_v if self.atr_stop_mult else 0.0
            return TradeSignal(signal=Signal.BUY, symbol=self.symbol, token=self.token,
                               entry_price=close, stop_loss=round(self._stop_loss, 2), target=0.0,
                               atr=round(atr_v, 2), confidence=min(100.0, abs(move_pct) * 20),
                               reason=f"Intraday momentum LONG | first30m {move_pct:+.2f}% -> hold to close")
        else:
            self._position, self._entry_price = "short", close
            self._stop_loss = close + self.atr_stop_mult * atr_v if self.atr_stop_mult else 0.0
            return TradeSignal(signal=Signal.SELL, symbol=self.symbol, token=self.token,
                               entry_price=close, stop_loss=round(self._stop_loss, 2), target=0.0,
                               atr=round(atr_v, 2), confidence=min(100.0, abs(move_pct) * 20),
                               reason=f"Intraday momentum SHORT | first30m {move_pct:+.2f}% -> hold to close")


class GapFadeStrategy(StrategyBase):
    """
    Config:
        gap_min_pct     : float = 1.0    (only fade gaps at least this big, %)
        gap_max_pct     : float = 6.0    (skip huge gaps — likely news/real repricing)
        target_fill     : float = 0.5    (target = close this fraction of the gap)
        atr_period      : int   = 14
        atr_stop_mult   : float = 1.0    (stop this many ATR beyond the entry, away from fill)
        entry_by_time   : str   = "10:00" (only enter on bars up to this time — fade early)
        min_price       : float = 50.0
    Fades the opening gap toward the prior close; exit on target fill, stop, or EOD.
    """

    def __init__(self, symbol: str, token: str, **kwargs) -> None:
        super().__init__(symbol, token, **kwargs)
        p = kwargs
        self.gap_min_pct = float(p.get("gap_min_pct", 1.0))
        self.gap_max_pct = float(p.get("gap_max_pct", 6.0))
        self.target_fill = float(p.get("target_fill", 0.5))
        self.atr_period = int(p.get("atr_period", 14))
        self.atr_stop_mult = float(p.get("atr_stop_mult", 1.0))
        self.min_price = float(p.get("min_price", 50.0))
        ebt = p.get("entry_by_time") or "10:00"
        self.entry_by_time = datetime.strptime(ebt, "%H:%M").time()

        self._position = None
        self._entry_price = 0.0
        self._stop_loss = 0.0
        self._target = 0.0
        self._entered_today = False

    def reset(self) -> None:
        self._position = None
        self._entry_price = 0.0
        self._stop_loss = 0.0
        self._target = 0.0
        self._entered_today = False

    def force_exit(self) -> None:
        self._position = None
        self._entry_price = 0.0
        self._stop_loss = 0.0
        self._target = 0.0

    def discard_pending_entry(self) -> None:
        self.force_exit()

    def get_current_stop(self):
        return self._stop_loss if self._position is not None else None

    def get_current_target(self):
        return self._target if self._position is not None else None

    def get_position_direction(self):
        return self._position

    def get_entry_price(self):
        return self._entry_price if self._position is not None else None

    def set_sector_momentum(self, pct: float) -> None:
        pass

    def on_candle_close(self, candle, history) -> TradeSignal:
        hold = TradeSignal(signal=Signal.HOLD, symbol=self.symbol, token=self.token)
        if self._position is not None:
            return hold  # stop/target enforced intrabar by the backtester; EOD squares off
        if self._entered_today:
            return hold

        today, cts = _today(history, candle)
        if today is None or cts is None or len(today) < 1:
            return hold
        if cts.time() > self.entry_by_time:
            self._entered_today = True
            return hold

        # prior day's close = last row of history BEFORE today
        h = history.copy()
        h["timestamp"] = pd.to_datetime(h["timestamp"], errors="coerce")
        prior = h[h["timestamp"].dt.date < cts.date()]
        if prior.empty:
            return hold
        prev_close = float(prior["close"].iloc[-1])
        today_open = float(today["open"].iloc[0])
        close = float(candle["close"])
        if prev_close <= 0 or close < self.min_price:
            return hold
        gap_pct = (today_open - prev_close) / prev_close * 100
        if not (self.gap_min_pct <= abs(gap_pct) <= self.gap_max_pct):
            self._entered_today = True
            return hold

        atr_v = float(atr(history["high"], history["low"], history["close"], self.atr_period).iloc[-1])
        self._entered_today = True
        # fill target = close `target_fill` of the way from open back to prev_close
        fill_target = today_open - self.target_fill * (today_open - prev_close)
        if gap_pct > 0:   # gap up -> fade short
            self._position, self._entry_price = "short", close
            self._target = fill_target
            self._stop_loss = close + self.atr_stop_mult * atr_v
            if self._target >= close:   # target must be below entry for a short
                self._position = None
                return hold
            return TradeSignal(signal=Signal.SELL, symbol=self.symbol, token=self.token,
                               entry_price=close, stop_loss=round(self._stop_loss, 2),
                               target=round(self._target, 2), atr=round(atr_v, 2),
                               confidence=min(100.0, abs(gap_pct) * 15),
                               reason=f"Gap-fade SHORT | gap {gap_pct:+.2f}% -> fade to prev close")
        else:             # gap down -> fade long
            self._position, self._entry_price = "long", close
            self._target = fill_target
            self._stop_loss = close - self.atr_stop_mult * atr_v
            if self._target <= close:
                self._position = None
                return hold
            return TradeSignal(signal=Signal.BUY, symbol=self.symbol, token=self.token,
                               entry_price=close, stop_loss=round(self._stop_loss, 2),
                               target=round(self._target, 2), atr=round(atr_v, 2),
                               confidence=min(100.0, abs(gap_pct) * 15),
                               reason=f"Gap-fade LONG | gap {gap_pct:+.2f}% -> fade to prev close")


class PrevDayMomentumStrategy(StrategyBase):
    """
    Cross-sectional overnight-momentum continuation (user idea):

    Rank yesterday's closing-momentum movers and trade them next-day intraday in
    the SAME direction — long yesterday's strong closers, short yesterday's weak
    ones. The strategy emits an entry at the day's open with
    confidence = |yesterday's move|, so the portfolio backtester's per-candle
    confidence ranking naturally fills the limited slots with the BIGGEST movers
    (i.e. it acts as the screener). Exit is EOD square-off, an ATR target, or an
    ATR stop.

    "Closing momentum" is captured by two prior-day measures, both must agree:
      - prev_ret_pct    : yesterday's return (close vs the prior close)
      - close_strength  : where yesterday's close sat in its day range
                          (near 1.0 = closed near the high = strong; near 0 = weak)

    Config:
        up_threshold_pct   : float = 2.0   (long if prev-day return >= this)
        min_close_strength : float = 0.7   (long only if closed in top 30% of range;
                                            short if closed in bottom 30%)
        allow_short        : bool  = True
        atr_period         : int   = 14
        atr_stop_mult      : float = 1.0
        atr_target_mult    : float = 2.0   (0 = no target, rely on EOD)
        entry_by_time      : str   = "09:45" (enter only on the first bar or two)
        min_price          : float = 50.0
    """

    def __init__(self, symbol: str, token: str, **kwargs) -> None:
        super().__init__(symbol, token, **kwargs)
        p = kwargs
        self.up_threshold_pct = float(p.get("up_threshold_pct", 2.0))
        self.min_close_strength = float(p.get("min_close_strength", 0.7))
        self.allow_short = bool(p.get("allow_short", True))
        self.atr_period = int(p.get("atr_period", 14))
        self.atr_stop_mult = float(p.get("atr_stop_mult", 1.0))
        self.atr_target_mult = float(p.get("atr_target_mult", 2.0))
        self.min_price = float(p.get("min_price", 50.0))
        # reversal=True flips the bet: FADE yesterday's strong movers (short the
        # strong closers, buy the weak ones) — the short-term reversal effect,
        # the mirror of continuation.
        self.reversal = bool(p.get("reversal", False))
        ebt = p.get("entry_by_time") or "09:45"
        self.entry_by_time = datetime.strptime(ebt, "%H:%M").time()

        self._position = None
        self._entry_price = 0.0
        self._stop_loss = 0.0
        self._target = 0.0
        self._entered_today = False

    def reset(self) -> None:
        self._position = None
        self._entry_price = 0.0
        self._stop_loss = 0.0
        self._target = 0.0
        self._entered_today = False

    def force_exit(self) -> None:
        self._position = None
        self._entry_price = 0.0
        self._stop_loss = 0.0
        self._target = 0.0

    def discard_pending_entry(self) -> None:
        self.force_exit()

    def get_current_stop(self):
        return self._stop_loss if self._position is not None else None

    def get_current_target(self):
        return self._target if (self._position is not None and self._target) else None

    def get_position_direction(self):
        return self._position

    def get_entry_price(self):
        return self._entry_price if self._position is not None else None

    def set_sector_momentum(self, pct: float) -> None:
        pass

    def on_candle_close(self, candle, history) -> TradeSignal:
        hold = TradeSignal(signal=Signal.HOLD, symbol=self.symbol, token=self.token)
        if self._position is not None:
            return hold  # stop/target intrabar + EOD square-off handled by backtester
        if self._entered_today:
            return hold

        today, cts = _today(history, candle)
        if today is None or cts is None:
            return hold
        if cts.time() > self.entry_by_time:
            self._entered_today = True
            return hold

        h = history.copy()
        h["timestamp"] = pd.to_datetime(h["timestamp"], errors="coerce")
        prior = h[h["timestamp"].dt.date < cts.date()]
        if prior.empty:
            return hold
        prior_dates = prior["timestamp"].dt.date
        last_day = prior_dates.iloc[-1]
        pday = prior[prior_dates == last_day]
        if len(pday) < 2:
            return hold
        p_open = float(pday["open"].iloc[0])
        p_high = float(pday["high"].max())
        p_low = float(pday["low"].min())
        p_close = float(pday["close"].iloc[-1])
        rng = p_high - p_low
        if rng <= 0 or p_open <= 0:
            return hold
        prev_ret_pct = (p_close - p_open) / p_open * 100
        close_strength = (p_close - p_low) / rng   # 1 = closed at high, 0 = at low

        close = float(candle["close"])
        if close < self.min_price:
            self._entered_today = True
            return hold
        atr_v = float(atr(history["high"], history["low"], history["close"], self.atr_period).iloc[-1])
        if atr_v <= 0:
            return hold

        strong_up = prev_ret_pct >= self.up_threshold_pct and close_strength >= self.min_close_strength
        weak_down = prev_ret_pct <= -self.up_threshold_pct and close_strength <= (1 - self.min_close_strength)
        conf = min(100.0, abs(prev_ret_pct) * 15)
        tag = "reversal" if self.reversal else "momentum"

        def enter(direction: str) -> TradeSignal:
            self._entered_today = True
            self._position, self._entry_price = direction, close
            if direction == "long":
                self._stop_loss = close - self.atr_stop_mult * atr_v
                self._target = close + self.atr_target_mult * atr_v if self.atr_target_mult else 0.0
                sig = Signal.BUY
            else:
                self._stop_loss = close + self.atr_stop_mult * atr_v
                self._target = close - self.atr_target_mult * atr_v if self.atr_target_mult else 0.0
                sig = Signal.SELL
            return TradeSignal(signal=sig, symbol=self.symbol, token=self.token,
                               entry_price=close, stop_loss=round(self._stop_loss, 2),
                               target=round(self._target, 2), atr=round(atr_v, 2), confidence=conf,
                               reason=f"PrevDay {tag} {direction.upper()} | y'day {prev_ret_pct:+.2f}% cs {close_strength:.2f}")

        if strong_up:
            # continuation -> long the strong closer; reversal -> short it
            direction = "short" if self.reversal else "long"
            if direction == "long" or self.allow_short:
                return enter(direction)
        if weak_down:
            # continuation -> short the weak closer; reversal -> long (bounce) it
            direction = "long" if self.reversal else "short"
            if direction == "long" or self.allow_short:
                return enter(direction)

        self._entered_today = True
        return hold


class GapFirstCandleStrategy(StrategyBase):
    """
    The popular NSE "big gap + first-candle" strategy (trade the opening move's
    direction on gapped stocks, confirmed by the first candle):

      - Pre-select stocks gapping >= gap_min_pct (either way) vs prior close.
      - On the first bar of the day: green candle (close>open) -> LONG,
        red -> SHORT (continuation of the opening thrust).
      - Stop = the first candle's low (long) / high (short).
      - Target = risk * rr_target (1:2 by default); else exit EOD.
      - confidence = |gap%| so the portfolio ranks the biggest gappers first.

    Config: gap_min_pct=2.0, gap_max_pct=8.0, rr_target=2.0, min_price=50.
    """

    def __init__(self, symbol: str, token: str, **kwargs) -> None:
        super().__init__(symbol, token, **kwargs)
        p = kwargs
        self.gap_min_pct = float(p.get("gap_min_pct", 2.0))
        self.gap_max_pct = float(p.get("gap_max_pct", 8.0))
        self.rr_target = float(p.get("rr_target", 2.0))
        self.min_price = float(p.get("min_price", 50.0))
        self._position = None
        self._entry_price = 0.0
        self._stop_loss = 0.0
        self._target = 0.0
        self._entered_today = False

    def reset(self):
        self._position = None; self._entry_price = 0.0; self._stop_loss = 0.0
        self._target = 0.0; self._entered_today = False

    def force_exit(self):
        self._position = None; self._entry_price = 0.0; self._stop_loss = 0.0; self._target = 0.0

    def discard_pending_entry(self):
        self.force_exit()

    def get_current_stop(self):
        return self._stop_loss if self._position is not None else None

    def get_current_target(self):
        return self._target if (self._position is not None and self._target) else None

    def get_position_direction(self):
        return self._position

    def get_entry_price(self):
        return self._entry_price if self._position is not None else None

    def set_sector_momentum(self, pct): pass

    def on_candle_close(self, candle, history) -> TradeSignal:
        hold = TradeSignal(signal=Signal.HOLD, symbol=self.symbol, token=self.token)
        if self._position is not None:
            return hold
        if self._entered_today:
            return hold
        today, cts = _today(history, candle)
        if today is None or cts is None or len(today) < 1:
            return hold
        # Only act on the FIRST bar of the day.
        first_ts = today["timestamp"].iloc[0]
        if pd.to_datetime(candle.get("timestamp")) != first_ts:
            return hold
        self._entered_today = True

        h = history.copy(); h["timestamp"] = pd.to_datetime(h["timestamp"], errors="coerce")
        prior = h[h["timestamp"].dt.date < cts.date()]
        if prior.empty:
            return hold
        prev_close = float(prior["close"].iloc[-1])
        o = float(candle["open"]); c = float(candle["close"])
        hi = float(candle["high"]); lo = float(candle["low"])
        if prev_close <= 0 or c < self.min_price:
            return hold
        gap = (o - prev_close) / prev_close * 100
        if not (self.gap_min_pct <= abs(gap) <= self.gap_max_pct):
            return hold
        conf = min(100.0, abs(gap) * 12)
        if c > o:   # green first candle -> long
            risk = c - lo
            if risk <= 0:
                return hold
            self._position, self._entry_price, self._stop_loss = "long", c, lo
            self._target = c + self.rr_target * risk if self.rr_target else 0.0
            return TradeSignal(signal=Signal.BUY, symbol=self.symbol, token=self.token,
                               entry_price=c, stop_loss=round(lo, 2), target=round(self._target, 2),
                               confidence=conf, reason=f"Gap+firstcandle LONG | gap {gap:+.2f}% green")
        elif c < o:  # red first candle -> short
            risk = hi - c
            if risk <= 0:
                return hold
            self._position, self._entry_price, self._stop_loss = "short", c, hi
            self._target = c - self.rr_target * risk if self.rr_target else 0.0
            return TradeSignal(signal=Signal.SELL, symbol=self.symbol, token=self.token,
                               entry_price=c, stop_loss=round(hi, 2), target=round(self._target, 2),
                               confidence=conf, reason=f"Gap+firstcandle SHORT | gap {gap:+.2f}% red")
        return hold


class AfternoonBreakoutStrategy(StrategyBase):
    """
    The "2 PM breakout" idea: the morning chops, then the afternoon picks a
    direction. Mark the high/low of the pre-decision window, then trade a break
    of that range into the close.

    Config:
        decision_time : str   = "14:00" (mark the day's range up to here)
        rr_target     : float = 1.5     (target = risk * this; else EOD)
        min_price     : float = 50.0
    Entry on the first bar after decision_time that closes beyond the pre-2pm
    high (long) or low (short). Stop = opposite side of that range. Exit EOD.
    """

    def __init__(self, symbol: str, token: str, **kwargs) -> None:
        super().__init__(symbol, token, **kwargs)
        p = kwargs
        from datetime import datetime as _dt
        self.decision_time = _dt.strptime(p.get("decision_time") or "14:00", "%H:%M").time()
        self.rr_target = float(p.get("rr_target", 1.5))
        self.min_price = float(p.get("min_price", 50.0))
        self._position = None
        self._entry_price = 0.0
        self._stop_loss = 0.0
        self._target = 0.0
        self._entered_today = False

    def reset(self):
        self._position = None; self._entry_price = 0.0; self._stop_loss = 0.0
        self._target = 0.0; self._entered_today = False

    def force_exit(self):
        self._position = None; self._entry_price = 0.0; self._stop_loss = 0.0; self._target = 0.0

    def discard_pending_entry(self):
        self.force_exit()

    def get_current_stop(self):
        return self._stop_loss if self._position is not None else None

    def get_current_target(self):
        return self._target if (self._position is not None and self._target) else None

    def get_position_direction(self):
        return self._position

    def get_entry_price(self):
        return self._entry_price if self._position is not None else None

    def set_sector_momentum(self, pct): pass

    def on_candle_close(self, candle, history) -> TradeSignal:
        hold = TradeSignal(signal=Signal.HOLD, symbol=self.symbol, token=self.token)
        if self._position is not None or self._entered_today:
            return hold
        today, cts = _today(history, candle)
        if today is None or cts is None:
            return hold
        if cts.time() < self.decision_time:
            return hold
        # pre-decision range = today's bars strictly before decision_time
        pre = today[today["timestamp"].dt.time < self.decision_time]
        if len(pre) < 2:
            return hold
        rng_hi = float(pre["high"].max()); rng_lo = float(pre["low"].min())
        c = float(candle["close"])
        if c < self.min_price:
            return hold
        if c > rng_hi:
            risk = c - rng_lo
            if risk <= 0:
                return hold
            self._entered_today = True
            self._position, self._entry_price, self._stop_loss = "long", c, rng_lo
            self._target = c + self.rr_target * risk if self.rr_target else 0.0
            return TradeSignal(signal=Signal.BUY, symbol=self.symbol, token=self.token,
                               entry_price=c, stop_loss=round(rng_lo, 2), target=round(self._target, 2),
                               confidence=60.0, reason="Afternoon breakout LONG (>pre-2pm high)")
        if c < rng_lo:
            risk = rng_hi - c
            if risk <= 0:
                return hold
            self._entered_today = True
            self._position, self._entry_price, self._stop_loss = "short", c, rng_hi
            self._target = c - self.rr_target * risk if self.rr_target else 0.0
            return TradeSignal(signal=Signal.SELL, symbol=self.symbol, token=self.token,
                               entry_price=c, stop_loss=round(rng_hi, 2), target=round(self._target, 2),
                               confidence=60.0, reason="Afternoon breakout SHORT (<pre-2pm low)")
        return hold


class RegimeSwitchedPrevDayStrategy(StrategyBase):
    """
    Synthesis of the user's "trade yesterday's leaders" idea + our finding that
    fading works better in weak markets, gated by a broad-market regime:

      - Detect yesterday's strong movers (prev-day return + close strength).
      - If MARKET REGIME is up (+1): CONTINUATION — go with the mover, but only
        on an intraday CONFIRMATION (break of yesterday's high for longs / low
        for shorts) WITH above-average volume.
      - If regime is down/chop (<=0): FADE the mover at the open (short a strong
        up-closer / long a weak down-closer) — the reversal that scored best.

    confidence = |prev-day return| so the portfolio ranks the biggest movers
    into its limited slots (acts as the screener). ATR stop + target; EOD exit.

    Config:
        up_threshold_pct=2.0, min_close_strength=0.7, allow_short=True,
        vol_mult=1.0 (entry-bar volume >= this * 20-bar avg; 0 = disabled),
        atr_period=14, atr_stop_mult=1.5, atr_target_mult=2.0,
        entry_by_time="14:30", min_price=50.
    """

    def __init__(self, symbol: str, token: str, **kwargs) -> None:
        super().__init__(symbol, token, **kwargs)
        p = kwargs
        self.up_threshold_pct = float(p.get("up_threshold_pct", 2.0))
        self.min_close_strength = float(p.get("min_close_strength", 0.7))
        self.allow_short = bool(p.get("allow_short", True))
        self.vol_mult = float(p.get("vol_mult", 1.0))
        self.atr_period = int(p.get("atr_period", 14))
        self.atr_stop_mult = float(p.get("atr_stop_mult", 1.5))
        self.atr_target_mult = float(p.get("atr_target_mult", 2.0))
        self.min_price = float(p.get("min_price", 50.0))
        et = p.get("entry_by_time") or "14:30"
        self.entry_by_time = datetime.strptime(et, "%H:%M").time()

        self._regime = 0
        self._position = None
        self._entry_price = 0.0
        self._stop_loss = 0.0
        self._target = 0.0
        self._entered_today = False
        # per-day cache
        self._cache_date = None
        self._y_hi = self._y_lo = None
        self._mover = None       # "up" | "down" | None
        self._conf = 0.0

    def set_market_regime(self, regime: int) -> None:
        self._regime = int(regime)

    def reset(self):
        self._position = None; self._entry_price = 0.0; self._stop_loss = 0.0
        self._target = 0.0; self._entered_today = False
        # NOTE: do not clear _regime (set by orchestrator after reset)

    def force_exit(self):
        self._position = None; self._entry_price = 0.0; self._stop_loss = 0.0; self._target = 0.0

    def discard_pending_entry(self):
        self.force_exit()

    def get_current_stop(self):
        return self._stop_loss if self._position is not None else None

    def get_current_target(self):
        return self._target if (self._position is not None and self._target) else None

    def get_position_direction(self):
        return self._position

    def get_entry_price(self):
        return self._entry_price if self._position is not None else None

    def set_sector_momentum(self, pct): pass

    def _enter(self, direction, close, atr_v, reason):
        self._entered_today = True
        self._position, self._entry_price = direction, close
        if direction == "long":
            self._stop_loss = close - self.atr_stop_mult * atr_v
            self._target = close + self.atr_target_mult * atr_v if self.atr_target_mult else 0.0
            sig = Signal.BUY
        else:
            self._stop_loss = close + self.atr_stop_mult * atr_v
            self._target = close - self.atr_target_mult * atr_v if self.atr_target_mult else 0.0
            sig = Signal.SELL
        return TradeSignal(signal=sig, symbol=self.symbol, token=self.token,
                           entry_price=close, stop_loss=round(self._stop_loss, 2),
                           target=round(self._target, 2), atr=round(atr_v, 2), confidence=self._conf,
                           reason=reason)

    def on_candle_close(self, candle, history) -> TradeSignal:
        hold = TradeSignal(signal=Signal.HOLD, symbol=self.symbol, token=self.token)
        if self._position is not None or self._entered_today:
            return hold
        today, cts = _today(history, candle)
        if today is None or cts is None:
            return hold
        if cts.time() > self.entry_by_time:
            self._entered_today = True
            return hold

        cur_date = cts.date()
        if self._cache_date != cur_date:
            self._cache_date = cur_date
            self._y_hi = self._y_lo = None; self._mover = None; self._conf = 0.0
            h = history.copy(); h["timestamp"] = pd.to_datetime(h["timestamp"], errors="coerce")
            prior = h[h["timestamp"].dt.date < cur_date]
            if not prior.empty:
                pdates = prior["timestamp"].dt.date
                pday = prior[pdates == pdates.iloc[-1]]
                if len(pday) >= 2:
                    p_open = float(pday["open"].iloc[0]); p_close = float(pday["close"].iloc[-1])
                    p_hi = float(pday["high"].max()); p_lo = float(pday["low"].min())
                    rng = p_hi - p_lo
                    if rng > 0 and p_open > 0:
                        prev_ret = (p_close - p_open) / p_open * 100
                        cs = (p_close - p_lo) / rng
                        self._y_hi, self._y_lo = p_hi, p_lo
                        self._conf = min(100.0, abs(prev_ret) * 15)
                        if prev_ret >= self.up_threshold_pct and cs >= self.min_close_strength:
                            self._mover = "up"
                        elif prev_ret <= -self.up_threshold_pct and cs <= (1 - self.min_close_strength):
                            self._mover = "down"

        if self._mover is None:
            self._entered_today = True
            return hold
        close = float(candle["close"])
        if close < self.min_price:
            self._entered_today = True
            return hold
        atr_v = float(atr(history["high"], history["low"], history["close"], self.atr_period).iloc[-1])
        if atr_v <= 0:
            return hold

        # volume confirmation (entry bar vs 20-bar avg)
        vol_ok = True
        if self.vol_mult > 0:
            vols = history["volume"]
            avg20 = float(vols.iloc[-20:].mean()) if len(vols) >= 5 else 0.0
            cur_vol = float(candle["volume"])
            vol_ok = avg20 > 0 and cur_vol >= self.vol_mult * avg20

        uptrend = self._regime > 0
        if uptrend:
            # CONTINUATION with breakout confirmation + volume
            if self._mover == "up" and close > self._y_hi and vol_ok:
                return self._enter("long", close, atr_v, f"Regime-UP continuation LONG (>y-high) conf{self._conf:.0f}")
            if self.allow_short and self._mover == "down" and close < self._y_lo and vol_ok:
                return self._enter("short", close, atr_v, f"Regime-UP continuation SHORT (<y-low) conf{self._conf:.0f}")
            return hold  # wait for confirmation later in the day
        else:
            # DOWN/CHOP: FADE the mover at the open (first eligible bar)
            if self._mover == "up" and self.allow_short:
                return self._enter("short", close, atr_v, f"Regime-DOWN fade SHORT (fade up-mover) conf{self._conf:.0f}")
            if self._mover == "down":
                return self._enter("long", close, atr_v, f"Regime-DOWN fade LONG (fade down-mover) conf{self._conf:.0f}")
            self._entered_today = True
            return hold
