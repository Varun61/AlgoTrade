"""
strategy/xpost_extra.py

Additional mechanizable intraday strategies from the shared X posts, so every
reasonably-testable idea gets a fair backtest:

  ORBRangeSweepStrategy   — the "1:1 RR ORB" idea: mark the first-15m range, then
                            on a break of the range trade toward a 1:1 target
                            (range width beyond the break), stop at the opposite
                            range edge. (The "which extreme formed first" nuance
                            needs sub-15m data — see notes; this is the standard
                            mechanical ORB-range version.)
  SupertrendStrengthStrategy — long stocks already up >= strength_pct from the
                            day's open by a cutoff time WITH Supertrend bullish;
                            ride to EOD with a Supertrend/ATR stop.
  InsideBarBreakoutStrategy  — NR/inside-day: when today opens inside yesterday's
                            range, trade a break of the prior-day high/low intraday.

All square off same day (intraday) via the backtester's EOD logic.
"""

from __future__ import annotations
from datetime import datetime

import pandas as pd

from .strategy_base import StrategyBase, TradeSignal, Signal
from .indicators import atr, supertrend
from .research_intraday import _today


class _OneTradeIntradayBase(StrategyBase):
    """Shared plumbing: one trade/day, stop+target tracked for the backtester."""
    def __init__(self, symbol, token, **kwargs):
        super().__init__(symbol, token, **kwargs)
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


class ORBRangeSweepStrategy(_OneTradeIntradayBase):
    """
    Config: orb_bars=1 (# opening 15m bars for the range), rr_target=1.0,
            entry_by_time="13:00", min_range_pct=0.2, max_range_pct=3.0, min_price=50.
    """
    def __init__(self, symbol, token, **kwargs):
        super().__init__(symbol, token, **kwargs)
        p = kwargs
        self.orb_bars = int(p.get("orb_bars", 1))
        self.rr_target = float(p.get("rr_target", 1.0))
        self.entry_by_time = datetime.strptime(p.get("entry_by_time") or "13:00", "%H:%M").time()
        self.min_range_pct = float(p.get("min_range_pct", 0.2))
        self.max_range_pct = float(p.get("max_range_pct", 3.0))
        self.min_price = float(p.get("min_price", 50.0))

    def on_candle_close(self, candle, history) -> TradeSignal:
        hold = TradeSignal(signal=Signal.HOLD, symbol=self.symbol, token=self.token)
        if self._position is not None or self._entered_today:
            return hold
        today, cts = _today(history, candle)
        if today is None or cts is None or len(today) <= self.orb_bars:
            return hold
        if cts.time() > self.entry_by_time:
            self._entered_today = True
            return hold
        rng = today.iloc[:self.orb_bars]
        hi = float(rng["high"].max()); lo = float(rng["low"].min())
        c = float(candle["close"])
        width = hi - lo
        if width <= 0 or c < self.min_price:
            return hold
        rpct = width / c * 100
        if not (self.min_range_pct <= rpct <= self.max_range_pct):
            self._entered_today = True
            return hold
        if c > hi:
            self._entered_today = True
            self._position, self._entry_price, self._stop_loss = "long", c, lo
            self._target = c + self.rr_target * width if self.rr_target else 0.0
            return TradeSignal(signal=Signal.BUY, symbol=self.symbol, token=self.token,
                               entry_price=c, stop_loss=round(lo, 2), target=round(self._target, 2),
                               confidence=60.0, reason="ORB range sweep LONG")
        if c < lo:
            self._entered_today = True
            self._position, self._entry_price, self._stop_loss = "short", c, hi
            self._target = c - self.rr_target * width if self.rr_target else 0.0
            return TradeSignal(signal=Signal.SELL, symbol=self.symbol, token=self.token,
                               entry_price=c, stop_loss=round(hi, 2), target=round(self._target, 2),
                               confidence=60.0, reason="ORB range sweep SHORT")
        return hold


class SupertrendStrengthStrategy(_OneTradeIntradayBase):
    """
    Long stocks already up >= strength_pct from the day's open by entry_by_time,
    with Supertrend bullish. Ride to EOD with a Supertrend-line / ATR stop.

    Config: strength_pct=3.0, st_period=10, st_mult=2.0, atr_period=14,
            atr_stop_mult=2.0, entry_by_time="11:30", allow_short=True, min_price=50.
    """
    def __init__(self, symbol, token, **kwargs):
        super().__init__(symbol, token, **kwargs)
        p = kwargs
        self.strength_pct = float(p.get("strength_pct", 3.0))
        self.st_period = int(p.get("st_period", 10))
        self.st_mult = float(p.get("st_mult", 2.0))
        self.atr_period = int(p.get("atr_period", 14))
        self.atr_stop_mult = float(p.get("atr_stop_mult", 2.0))
        self.entry_by_time = datetime.strptime(p.get("entry_by_time") or "11:30", "%H:%M").time()
        self.allow_short = bool(p.get("allow_short", True))
        self.min_price = float(p.get("min_price", 50.0))

    def on_candle_close(self, candle, history) -> TradeSignal:
        hold = TradeSignal(signal=Signal.HOLD, symbol=self.symbol, token=self.token)
        if self._position is not None or self._entered_today:
            return hold
        today, cts = _today(history, candle)
        if today is None or cts is None or len(today) < 2:
            return hold
        if cts.time() > self.entry_by_time:
            self._entered_today = True
            return hold
        day_open = float(today["open"].iloc[0])
        c = float(candle["close"])
        if day_open <= 0 or c < self.min_price:
            return hold
        move = (c - day_open) / day_open * 100
        st = supertrend(history["high"], history["low"], history["close"], self.st_period, self.st_mult)
        st_dir = int(st.iloc[-1])
        atr_v = float(atr(history["high"], history["low"], history["close"], self.atr_period).iloc[-1])
        if atr_v <= 0:
            return hold
        if move >= self.strength_pct and st_dir > 0:
            self._entered_today = True
            self._position, self._entry_price = "long", c
            self._stop_loss = c - self.atr_stop_mult * atr_v
            self._target = 0.0
            return TradeSignal(signal=Signal.BUY, symbol=self.symbol, token=self.token,
                               entry_price=c, stop_loss=round(self._stop_loss, 2), target=0.0,
                               confidence=min(100.0, move * 12),
                               reason=f"Supertrend+strength LONG | +{move:.1f}% ST up")
        if self.allow_short and move <= -self.strength_pct and st_dir < 0:
            self._entered_today = True
            self._position, self._entry_price = "short", c
            self._stop_loss = c + self.atr_stop_mult * atr_v
            self._target = 0.0
            return TradeSignal(signal=Signal.SELL, symbol=self.symbol, token=self.token,
                               entry_price=c, stop_loss=round(self._stop_loss, 2), target=0.0,
                               confidence=min(100.0, abs(move) * 12),
                               reason=f"Supertrend+strength SHORT | {move:.1f}% ST down")
        return hold


class InsideBarBreakoutStrategy(_OneTradeIntradayBase):
    """
    NR/inside-day breakout: when today's open is inside yesterday's range, trade a
    break of the prior-day high (long) / low (short) intraday.

    Config: rr_target=2.0, atr_period=14, entry_by_time="14:00",
            require_nr=True (only if yesterday was the narrowest range of last `nr_lookback` days),
            nr_lookback=7, min_price=50.
    """
    def __init__(self, symbol, token, **kwargs):
        super().__init__(symbol, token, **kwargs)
        p = kwargs
        self.rr_target = float(p.get("rr_target", 2.0))
        self.entry_by_time = datetime.strptime(p.get("entry_by_time") or "14:00", "%H:%M").time()
        self.require_nr = bool(p.get("require_nr", True))
        self.nr_lookback = int(p.get("nr_lookback", 7))
        self.min_price = float(p.get("min_price", 50.0))
        self._cache_date = None      # per-day cache of prior-day levels (perf)
        self._y_hi = self._y_lo = None
        self._nr_ok = False
        self._inside_open = False

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

        # Compute prior-day levels ONCE per session (cached) — the groupby is
        # expensive to redo on every bar.
        cur_date = cts.date()
        if self._cache_date != cur_date:
            self._cache_date = cur_date
            self._y_hi = self._y_lo = None
            self._nr_ok = False
            self._inside_open = False
            h = history.copy(); h["timestamp"] = pd.to_datetime(h["timestamp"], errors="coerce")
            prior = h[h["timestamp"].dt.date < cur_date]
            if not prior.empty:
                pj = prior.copy(); pj["d"] = pj["timestamp"].dt.date
                daily = pj.groupby("d").agg(hi=("high", "max"), lo=("low", "min"))
                need = (self.nr_lookback + 1) if self.require_nr else 1
                if len(daily) >= need:
                    y_hi = float(daily["hi"].iloc[-1]); y_lo = float(daily["lo"].iloc[-1])
                    y_range = y_hi - y_lo
                    if y_range > 0:
                        nr_ok = True
                        if self.require_nr:
                            recent = (daily["hi"] - daily["lo"]).iloc[-self.nr_lookback:]
                            nr_ok = y_range <= recent.min() + 1e-9   # yesterday the narrowest
                        t_open = float(today["open"].iloc[0])
                        self._y_hi, self._y_lo = y_hi, y_lo
                        self._nr_ok = nr_ok
                        self._inside_open = (y_lo <= t_open <= y_hi)

        if self._y_hi is None or not self._nr_ok or not self._inside_open:
            self._entered_today = True
            return hold
        y_hi, y_lo = self._y_hi, self._y_lo
        c = float(candle["close"])
        if c < self.min_price:
            return hold
        if c > y_hi:
            self._entered_today = True
            risk = y_hi - y_lo
            self._position, self._entry_price, self._stop_loss = "long", c, y_lo
            self._target = c + self.rr_target * risk if self.rr_target else 0.0
            return TradeSignal(signal=Signal.BUY, symbol=self.symbol, token=self.token,
                               entry_price=c, stop_loss=round(y_lo, 2), target=round(self._target, 2),
                               confidence=60.0, reason="Inside-bar breakout LONG (>prev-day high)")
        if c < y_lo:
            self._entered_today = True
            risk = y_hi - y_lo
            self._position, self._entry_price, self._stop_loss = "short", c, y_hi
            self._target = c - self.rr_target * risk if self.rr_target else 0.0
            return TradeSignal(signal=Signal.SELL, symbol=self.symbol, token=self.token,
                               entry_price=c, stop_loss=round(y_hi, 2), target=round(self._target, 2),
                               confidence=60.0, reason="Inside-bar breakout SHORT (<prev-day low)")
        return hold
