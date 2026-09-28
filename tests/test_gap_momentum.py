"""
Unit tests for strategy/gap_momentum.GapMomentumStrategy.
These prove the fresh strategy behaves EXACTLY as specified — so a backtest of it
can't be dismissed as a hidden bug.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datetime import datetime, timedelta
import pandas as pd
import pytest

from strategy.gap_momentum import GapMomentumStrategy
from strategy.strategy_base import Signal


def _hist(gap_pct=3.0, orb_vol=6000, prior_vol=1000, break_close=None, break_time="09:45"):
    """Prior day (close 100) + today: gap open, ORB first bar, then a breakout bar."""
    rows = []
    d1 = datetime(2024, 6, 3, 9, 15)
    for i in range(25):
        rows.append({"timestamp": d1 + timedelta(minutes=15 * i), "open": 100, "high": 100.5,
                     "low": 99.5, "close": 100, "volume": prior_vol})
    # today
    t_open = 100 * (1 + gap_pct / 100)
    d2 = datetime(2024, 6, 4, 9, 15)
    # ORB bar (first bar): range around the open
    rows.append({"timestamp": d2, "open": t_open, "high": t_open + 1.0, "low": t_open - 1.0,
                 "close": t_open, "volume": orb_vol})
    # a breakout bar before entry_by_time
    if break_close is not None:
        bt = datetime.strptime(break_time, "%H:%M").time()
        ts = d2.replace(hour=bt.hour, minute=bt.minute)
        rows.append({"timestamp": ts, "open": t_open, "high": max(t_open, break_close) + 0.5,
                     "low": min(t_open, break_close) - 0.5, "close": break_close, "volume": orb_vol})
    return pd.DataFrame(rows)


def _mk(**over):
    kw = dict(symbol="T", token="1", gap_min_pct=1.0, gap_max_pct=10.0, orb_bars=1,
              rel_vol_min=2.0, rr_target=2.0, entry_by_time="11:00", min_price=1.0)
    kw.update(over)
    return GapMomentumStrategy(**kw)


def test_long_on_gapup_highvol_breakout():
    # gap +3%, ORB bar ~103 (hi ~104), breakout bar closes 105 > ORB high -> LONG
    hist = _hist(gap_pct=3.0, orb_vol=6000, break_close=105.0)
    sig = _mk().on_candle_close(hist.iloc[-1], hist)
    assert sig.signal == Signal.BUY
    assert sig.stop_loss < sig.entry_price < sig.target


def test_short_on_gapdown_highvol_breakdown():
    hist = _hist(gap_pct=-3.0, orb_vol=6000, break_close=95.0)  # gap down, breaks ORB low
    sig = _mk().on_candle_close(hist.iloc[-1], hist)
    assert sig.signal == Signal.SELL
    assert sig.stop_loss > sig.entry_price > sig.target


def test_no_trade_when_gap_too_small():
    hist = _hist(gap_pct=0.3, orb_vol=6000, break_close=105.0)  # gap < 1%
    assert _mk().on_candle_close(hist.iloc[-1], hist).signal == Signal.HOLD


def test_no_trade_when_volume_not_elevated():
    # opening volume ~= prior avg -> rel_vol < 2 -> not "in play"
    hist = _hist(gap_pct=3.0, orb_vol=1000, prior_vol=1000, break_close=105.0)
    assert _mk().on_candle_close(hist.iloc[-1], hist).signal == Signal.HOLD


def test_no_trade_without_breakout():
    # gap + volume fine, but price never breaks the opening range high
    hist = _hist(gap_pct=3.0, orb_vol=6000, break_close=103.2)  # stays inside ORB (hi ~104)
    assert _mk().on_candle_close(hist.iloc[-1], hist).signal == Signal.HOLD


def test_no_entry_after_cutoff():
    hist = _hist(gap_pct=3.0, orb_vol=6000, break_close=105.0, break_time="13:00")  # past 11:00
    assert _mk(entry_by_time="11:00").on_candle_close(hist.iloc[-1], hist).signal == Signal.HOLD


def test_target_uses_rr_and_range_stop():
    hist = _hist(gap_pct=3.0, orb_vol=6000, break_close=105.0)
    strat = _mk(rr_target=2.0)
    sig = strat.on_candle_close(hist.iloc[-1], hist)
    risk = sig.entry_price - sig.stop_loss
    assert sig.target == pytest.approx(sig.entry_price + 2.0 * risk, abs=0.01)
