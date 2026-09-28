"""
strategy/gap_momentum.py

A FRESH, deliberately-simple "stocks in play" day-trading strategy — written
from scratch (does NOT reuse the ORB/EMA/VWAP confidence code) so its logic is
fully auditable and we can trust a backtest of it isn't hiding a bug.

Idea (the user's): each morning, only trade stocks that look genuinely "in play"
— i.e. they GAPPED and are trading on UNUSUALLY HIGH VOLUME — and take the
opening-range breakout in the gap's direction.

Rules (all computable at entry time, no lookahead):
  1. Gap: |today_open - prev_close| / prev_close >= gap_min_pct.
  2. In play: opening relative volume (today's first-bar volume vs its own recent
     20-bar average) >= rel_vol_min.
  3. Opening range = high/low of the first `orb_bars` 15-min bars of the day.
  4. Entry (only before entry_by_time):
       gap UP  + price closes above the opening-range high -> LONG
       gap DOWN + price closes below the opening-range low  -> SHORT
     Stop = opposite side of the opening range. Target = entry +/- rr_target * risk.
  5. confidence = relative volume, so the portfolio backtester fills its limited
     slots with the MOST in-play names each day (this is the daily "screener").
  6. One trade per symbol per day; exit on stop/target/EOD.

Nothing here talks to the API — pure signal logic.
"""
from __future__ import annotations
from datetime import datetime

import pandas as pd

from .strategy_base import StrategyBase, TradeSignal, Signal


class GapMomentumStrategy(StrategyBase):
    """
    Config:
        gap_min_pct   = 1.0    (minimum overnight gap %, either direction)
        gap_max_pct   = 10.0   (skip crazy gaps — likely news/halts)
        orb_bars      = 1      (# opening 15-min bars for the range)
        rel_vol_min   = 2.0    (opening volume must be >= this x its 20-bar avg)
        rr_target     = 2.0    (target = risk * this)
        entry_by_time = "11:00" (no new entries after this)
        min_price     = 50.0
        allow_short   = True
    """

    def __init__(self, symbol: str, token: str, **kwargs) -> None:
        super().__init__(symbol, token, **kwargs)
        p = kwargs
        self.gap_min_pct = float(p.get("gap_min_pct", 1.0))
        self.gap_max_pct = float(p.get("gap_max_pct", 10.0))
        self.orb_bars = int(p.get("orb_bars", 1))
        self.rel_vol_min = float(p.get("rel_vol_min", 2.0))
        self.rr_target = float(p.get("rr_target", 2.0))
        self.min_price = float(p.get("min_price", 50.0))
        self.allow_short = bool(p.get("allow_short", True))
        self.entry_by_time = datetime.strptime(p.get("entry_by_time") or "11:00", "%H:%M").time()

        self._position = None
        self._entry = 0.0
        self._stop = 0.0
        self._target = 0.0
        self._entered_today = False
        # per-day cache
        self._cache_date = None
        self._orb_hi = self._orb_lo = None
        self._gap = 0.0
        self._rel_vol = 0.0
        self._eligible = False

    # --- StrategyBase interface ---
    def reset(self):
        self._position = None; self._entry = 0.0; self._stop = 0.0
        self._target = 0.0; self._entered_today = False

    def force_exit(self): self.reset()
    def discard_pending_entry(self): self.reset()
    def get_current_stop(self): return self._stop if self._position else None
    def get_current_target(self): return self._target if self._position else None
    def get_position_direction(self): return self._position
    def get_entry_price(self): return self._entry if self._position else None
    def set_sector_momentum(self, pct): pass

    def on_candle_close(self, candle, history) -> TradeSignal:
        hold = TradeSignal(signal=Signal.HOLD, symbol=self.symbol, token=self.token)
        if self._position is not None or self._entered_today:
            return hold

        ts = pd.to_datetime(candle.get("timestamp"), errors="coerce")
        if pd.isna(ts):
            return hold
        if ts.time() > self.entry_by_time:
            self._entered_today = True
            return hold

        cur_date = ts.date()
        # ---- compute per-day features ONCE (gap, opening range, relative volume) ----
        if self._cache_date != cur_date:
            self._cache_date = cur_date
            self._orb_hi = self._orb_lo = None
            self._gap = self._rel_vol = 0.0
            self._eligible = False
            h = history.copy()
            h["timestamp"] = pd.to_datetime(h["timestamp"], errors="coerce")
            today = h[h["timestamp"].dt.date == cur_date]
            prior = h[h["timestamp"].dt.date < cur_date]
            if len(today) >= self.orb_bars and not prior.empty:
                prev_close = float(prior["close"].iloc[-1])
                t_open = float(today["open"].iloc[0])
                if prev_close > 0:
                    self._gap = (t_open - prev_close) / prev_close * 100
                    orb = today.iloc[:self.orb_bars]
                    self._orb_hi = float(orb["high"].max())
                    self._orb_lo = float(orb["low"].min())
                    # relative volume: today's opening bars vs trailing 20-bar avg
                    avg20 = float(h["volume"].iloc[-21:-1].mean()) if len(h) > 21 else 0.0
                    open_vol = float(orb["volume"].mean())
                    self._rel_vol = (open_vol / avg20) if avg20 > 0 else 0.0
                    self._eligible = (self.gap_min_pct <= abs(self._gap) <= self.gap_max_pct
                                      and self._rel_vol >= self.rel_vol_min)

        if not self._eligible or self._orb_hi is None:
            return hold
        close = float(candle["close"])
        if close < self.min_price:
            self._entered_today = True
            return hold

        conf = min(100.0, self._rel_vol * 20)  # rank by how "in play" it is

        # LONG: gap up + break of opening-range high
        if self._gap > 0 and close > self._orb_hi:
            risk = close - self._orb_lo
            if risk <= 0:
                return hold
            self._entered_today = True
            self._position, self._entry, self._stop = "long", close, self._orb_lo
            self._target = close + self.rr_target * risk
            return TradeSignal(signal=Signal.BUY, symbol=self.symbol, token=self.token,
                               entry_price=close, stop_loss=round(self._stop, 2),
                               target=round(self._target, 2), confidence=conf,
                               reason=f"Gap-momentum LONG | gap {self._gap:+.1f}% relvol {self._rel_vol:.1f}x")

        # SHORT: gap down + break of opening-range low
        if self.allow_short and self._gap < 0 and close < self._orb_lo:
            risk = self._orb_hi - close
            if risk <= 0:
                return hold
            self._entered_today = True
            self._position, self._entry, self._stop = "short", close, self._orb_hi
            self._target = close - self.rr_target * risk
            return TradeSignal(signal=Signal.SELL, symbol=self.symbol, token=self.token,
                               entry_price=close, stop_loss=round(self._stop, 2),
                               target=round(self._target, 2), confidence=conf,
                               reason=f"Gap-momentum SHORT | gap {self._gap:+.1f}% relvol {self._rel_vol:.1f}x")

        return hold
