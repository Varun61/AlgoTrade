"""
Unit tests for strategy/vwap_reversion.VWAPMeanReversionStrategy.

Entry gates and exit mechanics are tested via a hand-built history that puts
price a controlled number of ATRs away from the intraday VWAP in a low-ADX
(ranging) context, plus direct _check_exits() state tests.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from strategy.vwap_reversion import VWAPMeanReversionStrategy
from strategy.strategy_base import Signal


def _ranging_history(n=40, base=100.0, last_close=None, seed=1):
    """A gently oscillating (low-ADX, range-bound) single session, so VWAP sits
    near `base`. Optionally force the final close to `last_close`."""
    rng = np.random.default_rng(seed)
    t0 = datetime(2024, 3, 1, 9, 15)
    rows = []
    price = base
    for i in range(n):
        price = base + np.sin(i / 2.0) * 0.3 + rng.normal(0, 0.05)  # oscillate around base
        rows.append({"timestamp": t0 + timedelta(minutes=15 * i),
                     "open": price, "high": price + 0.15, "low": price - 0.15,
                     "close": price, "volume": 1000})
    if last_close is not None:
        rows[-1]["close"] = last_close
        rows[-1]["high"] = max(rows[-1]["high"], last_close + 0.15)
        rows[-1]["low"] = min(rows[-1]["low"], last_close - 0.15)
    return pd.DataFrame(rows)


def _mk(**over):
    kw = dict(symbol="TEST-EQ", token="1", candle_minutes=15, band_atr=1.5,
              stop_atr_mult=1.0, target_frac=0.8, max_adx=90.0, rsi_long_max=45.0,
              rsi_short_min=55.0, min_confidence=0.0, min_atr_pct=0.0, max_atr_pct=100.0,
              min_entry_time=None, min_vwap_bars=1)
    kw.update(over)
    return VWAPMeanReversionStrategy(**kw)


def test_no_signal_when_price_near_vwap():
    strat = _mk()
    hist = _ranging_history(last_close=100.0)   # right at VWAP
    sig = strat.on_candle_close(hist.iloc[-1], hist)
    assert sig.signal == Signal.HOLD


def test_short_when_stretched_far_above_vwap():
    # Force last close far above the ~100 VWAP so dist/ATR clears band_atr, with a
    # small ATR so a modest price gap is many ATRs. Low band + low RSI gate.
    strat = _mk(band_atr=1.0, rsi_short_min=50.0)
    hist = _ranging_history(last_close=103.0)
    sig = strat.on_candle_close(hist.iloc[-1], hist)
    assert sig.signal == Signal.SELL
    assert sig.target < sig.entry_price          # target is back toward VWAP (below)
    assert sig.stop_loss > sig.entry_price        # stop is above (further from VWAP)
    assert strat.get_position_direction() == "short"


def test_long_when_stretched_far_below_vwap():
    strat = _mk(band_atr=1.0, rsi_long_max=50.0)
    hist = _ranging_history(last_close=97.0)
    sig = strat.on_candle_close(hist.iloc[-1], hist)
    assert sig.signal == Signal.BUY
    assert sig.target > sig.entry_price
    assert sig.stop_loss < sig.entry_price
    assert strat.get_position_direction() == "long"


def test_high_adx_trend_blocks_entry():
    # A strong monotonic uptrend => high ADX => mean-reversion must NOT fire.
    t0 = datetime(2024, 3, 1, 9, 15)
    rows = []
    for i in range(40):
        price = 100 + i * 0.8  # steady strong trend
        rows.append({"timestamp": t0 + timedelta(minutes=15 * i), "open": price,
                     "high": price + 0.2, "low": price - 0.2, "close": price, "volume": 1000})
    hist = pd.DataFrame(rows)
    strat = _mk(band_atr=0.5, max_adx=25.0, rsi_short_min=50.0, rsi_long_max=50.0)
    sig = strat.on_candle_close(hist.iloc[-1], hist)
    assert sig.signal == Signal.HOLD


def test_rsi_gate_blocks_non_extreme_short():
    # Stretched above VWAP but RSI not overbought enough (require >= 95) => no short.
    strat = _mk(band_atr=1.0, rsi_short_min=95.0)
    hist = _ranging_history(last_close=103.0)
    sig = strat.on_candle_close(hist.iloc[-1], hist)
    assert sig.signal == Signal.HOLD


def test_min_entry_time_blocks_early():
    # 25 bars from 09:15 => last bar at 15:15; gate at 15:30 must block it,
    # while the same setup fires when the gate is disabled.
    hist = _ranging_history(n=25, last_close=103.0)
    assert _mk(band_atr=1.0, rsi_short_min=50.0, min_entry_time=None).on_candle_close(
        hist.iloc[-1], hist).signal == Signal.SELL
    blocked = _mk(band_atr=1.0, rsi_short_min=50.0, min_entry_time="15:30").on_candle_close(
        hist.iloc[-1], hist)
    assert blocked.signal == Signal.HOLD


def test_exit_long_on_target_and_stop():
    strat = _mk()
    strat._position, strat._entry_price = "long", 100.0
    strat._stop_loss, strat._target, strat._candles_held = 98.0, 103.0, 0
    assert strat._check_exits(103.5).signal == Signal.EXIT_LONG   # target
    strat._position, strat._candles_held = "long", 0
    assert strat._check_exits(97.5).signal == Signal.EXIT_LONG    # stop


def test_exit_short_on_target_and_stop():
    strat = _mk()
    strat._position, strat._entry_price = "short", 100.0
    strat._stop_loss, strat._target, strat._candles_held = 102.0, 97.0, 0
    assert strat._check_exits(96.5).signal == Signal.EXIT_SHORT   # target
    strat._position, strat._candles_held = "short", 0
    assert strat._check_exits(102.5).signal == Signal.EXIT_SHORT  # stop


def test_max_holding_forces_exit():
    strat = _mk(max_holding_candles=3)
    strat._position, strat._entry_price = "long", 100.0
    strat._stop_loss, strat._target, strat._candles_held = 90.0, 130.0, 0  # unreachable
    assert strat._check_exits(100.0).signal == Signal.HOLD   # held=1
    assert strat._check_exits(100.0).signal == Signal.HOLD   # held=2
    assert strat._check_exits(100.0).signal == Signal.EXIT_LONG  # held=3

def test_force_exit_resets_state():
    strat = _mk()
    strat._position, strat._entry_price = "long", 100.0
    strat._stop_loss, strat._target = 98.0, 103.0
    strat.force_exit()
    assert strat.get_position_direction() is None
    assert strat.get_current_stop() is None
    assert strat.get_current_target() is None
