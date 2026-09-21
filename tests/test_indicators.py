"""
Unit tests for strategy/indicators.py.

Expected values are hand-derived (see comments) so these tests catch actual
numerical regressions, not just "it ran without an exception."
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd
import pytest

from strategy.indicators import ema, rsi, atr, vwap, opening_range, ema_crossover


# ----------------------------------------------------------------------
# EMA
# ----------------------------------------------------------------------

def test_ema_matches_hand_calculation():
    # span=3 -> alpha = 2/(3+1) = 0.5, adjust=False:
    # y0=10; y1=0.5*12+0.5*10=11; y2=0.5*14+0.5*11=12.5
    s = pd.Series([10.0, 12.0, 14.0])
    result = ema(s, 3)
    assert result.tolist() == pytest.approx([10.0, 11.0, 12.5])


def test_ema_constant_series_stays_constant():
    s = pd.Series([50.0] * 10)
    result = ema(s, 5)
    assert result.tolist() == pytest.approx([50.0] * 10)


# ----------------------------------------------------------------------
# RSI
# ----------------------------------------------------------------------

def test_rsi_pure_uptrend_is_100_not_nan():
    """Regression test: avg_loss==0 for a strict uptrend must yield RSI=100,
    not NaN (NaN previously arose from replace(0, nan) on the RSI denominator
    and would silently fail every downstream `rsi_val < threshold` filter)."""
    s = pd.Series([100.0 + i for i in range(30)])
    result = rsi(s, 14)
    assert not result.tail(15).isna().any()
    assert result.tail(15).tolist() == pytest.approx([100.0] * 15)


def test_rsi_pure_downtrend_is_0():
    s = pd.Series([100.0 - i for i in range(30)])
    result = rsi(s, 14)
    assert not result.tail(15).isna().any()
    assert result.tail(15).tolist() == pytest.approx([0.0] * 15)


def test_rsi_flat_price_is_neutral_50():
    """No movement at all (avg_gain == avg_loss == 0) has no directional bias."""
    s = pd.Series([100.0] * 30)
    result = rsi(s, 14)
    assert result.tail(15).tolist() == pytest.approx([50.0] * 15)


def test_rsi_bounded_0_100_for_noisy_series():
    rng = np.random.default_rng(3)
    s = pd.Series(100 + np.cumsum(rng.normal(0, 1, 200)))
    result = rsi(s, 14).dropna()
    assert (result >= 0).all() and (result <= 100).all()


def test_rsi_alternating_series_stays_strictly_between_bounds():
    # With EWM (not SMA) smoothing, a perfectly alternating series does not
    # settle exactly at 50 (recency weighting makes it phase-dependent), but
    # since both gains and losses are always present it must never hit the
    # 0/100 extremes reserved for one-sided (pure trend) series.
    s = pd.Series([100, 101, 100, 101, 100, 101, 100, 101, 100, 101] * 3, dtype=float)
    result = rsi(s, 5).tail(15)  # skip the warmup transient where avg_loss is still 0
    assert (result > 0).all() and (result < 100).all()


# ----------------------------------------------------------------------
# ATR
# ----------------------------------------------------------------------

def test_atr_matches_hand_calculation():
    high  = pd.Series([10.0, 12.0, 11.0])
    low   = pd.Series([8.0,  9.0,  9.0])
    close = pd.Series([9.0,  11.0, 10.0])
    # TR0 = high0-low0 = 2 (prev_close NaN terms skipped by max(skipna=True))
    # TR1 = max(3, |12-9|=3, |9-9|=0) = 3
    # TR2 = max(2, |11-11|=0, |9-11|=2) = 2
    # alpha=1/3, adjust=False:
    # a0=2; a1=1/3*3+2/3*2=2.3333...; a2=1/3*2+2/3*2.3333...=2.2222...
    result = atr(high, low, close, period=3)
    assert result.tolist() == pytest.approx([2.0, 2.333333, 2.222222], abs=1e-5)


def test_atr_is_never_negative():
    rng = np.random.default_rng(1)
    n = 100
    close = pd.Series(100 + np.cumsum(rng.normal(0, 1, n)))
    high  = close + rng.random(n)
    low   = close - rng.random(n)
    result = atr(high, low, close, period=14)
    assert (result.dropna() >= 0).all()


# ----------------------------------------------------------------------
# VWAP
# ----------------------------------------------------------------------

def test_vwap_matches_hand_calculation_single_day():
    high  = pd.Series([10.0, 12.0])
    low   = pd.Series([8.0,  9.0])
    close = pd.Series([9.0,  11.0])
    vol   = pd.Series([100.0, 200.0])
    idx = pd.to_datetime(["2024-01-01 09:15", "2024-01-01 09:30"])
    high.index = low.index = close.index = vol.index = idx

    # tp0=(10+8+9)/3=9; tpv0=900
    # tp1=(12+9+11)/3=10.66667; tpv1=2133.333
    # vwap0 = 900/100 = 9
    # vwap1 = (900+2133.333)/(100+200) = 3033.333/300 = 10.111111
    result = vwap(high, low, close, vol)
    assert result.tolist() == pytest.approx([9.0, 10.111111], abs=1e-5)


def test_vwap_resets_at_new_calendar_day():
    idx = pd.to_datetime([
        "2024-01-01 09:15", "2024-01-01 09:30",   # day 1
        "2024-01-02 09:15",                        # day 2 (new session)
    ])
    high  = pd.Series([10.0, 12.0, 20.0], index=idx)
    low   = pd.Series([8.0,  9.0,  18.0], index=idx)
    close = pd.Series([9.0,  11.0, 19.0], index=idx)
    vol   = pd.Series([100.0, 200.0, 50.0], index=idx)

    result = vwap(high, low, close, vol)
    # Day 2's VWAP must equal day 2's own typical price (19), not a blended
    # cumulative average carried over from day 1 (which would be ~10.9).
    assert result.iloc[-1] == pytest.approx(19.0, abs=1e-9)


def test_vwap_falls_back_to_cumulative_for_non_datetime_index():
    high  = pd.Series([10.0, 12.0])
    low   = pd.Series([8.0,  9.0])
    close = pd.Series([9.0,  11.0])
    vol   = pd.Series([100.0, 200.0])
    result = vwap(high, low, close, vol)  # default RangeIndex
    assert result.tolist() == pytest.approx([9.0, 10.111111], abs=1e-5)


# ----------------------------------------------------------------------
# Opening range
# ----------------------------------------------------------------------

def test_opening_range_basic():
    df = pd.DataFrame({
        "high": [10.0, 12.0, 9.0, 11.0],
        "low":  [8.0,   9.0, 7.0,  8.5],
    })
    orb_high, orb_low = opening_range(df, n_candles=2)
    assert orb_high == 12.0   # max(10, 12)
    assert orb_low  == 8.0    # min(8, 9)


def test_opening_range_single_candle():
    df = pd.DataFrame({"high": [15.0, 20.0], "low": [10.0, 5.0]})
    orb_high, orb_low = opening_range(df, n_candles=1)
    assert orb_high == 15.0
    assert orb_low  == 10.0


# ----------------------------------------------------------------------
# EMA crossover
# ----------------------------------------------------------------------

def test_ema_crossover_flags_only_the_crossing_candle():
    # A clean downtrend-then-uptrend forces exactly one bullish cross.
    s = pd.Series([20, 18, 16, 14, 12, 10, 12, 14, 16, 18, 20, 22, 24, 26, 28], dtype=float)
    cross = ema_crossover(s, fast=2, slow=5)
    bullish_idx = cross[cross == 1].index.tolist()
    assert len(bullish_idx) >= 1
    # No two consecutive candles should both be flagged as "the" crossover
    assert all(b - a > 1 for a, b in zip(bullish_idx, bullish_idx[1:]))
