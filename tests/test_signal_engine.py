"""
Unit tests for strategy/signal_engine.py (ORBEMAVWAPStrategy).

Scoring/exit-management methods are tested directly with hand-set state so
each mechanism (breakeven, trailing stop, time stop, volatility gate) can be
verified in isolation from the full multi-indicator on_candle_close() pipeline.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from strategy.signal_engine import ORBEMAVWAPStrategy
from strategy.strategy_base import Signal


def make_strategy(**overrides):
    kwargs = dict(symbol="TEST-EQ", token="1")
    kwargs.update(overrides)
    return ORBEMAVWAPStrategy(**kwargs)


# ----------------------------------------------------------------------
# Confidence scoring — _score_long / _score_short
# ----------------------------------------------------------------------

def test_score_long_all_factors_hand_computed():
    strat = make_strategy()
    strat._orb_high = 100.0

    close, ema_f, ema_s, rsi_val, vwap_v = 100.5, 10.1, 10.0, 55.0, 100.2
    cur_vol, avg_vol = 2000.0, 1000.0
    sl, target = 98.0, 106.0   # risk=2.5, reward=5.5, rr=2.2

    score, factors = strat._score_long(close, ema_f, ema_s, rsi_val, vwap_v,
                                        cur_vol, avg_vol, sl, target)

    # 1. orb_breakout: (100.5-100)/100*100=0.5% -> min(0.5,2.0)=0.5 -> min(20,0.5*10)=5.0
    assert factors["orb_breakout"] == pytest.approx(5.0, abs=0.1)
    # 2. ema_trend: (10.1-10.0)/10.0*100=1.0% -> min(20, 1.0*50)=20.0 (saturated)
    assert factors["ema_trend"] == pytest.approx(20.0, abs=0.1)
    # 3. vwap_position: (100.5-100.2)/100.2*100=0.2994% in [0.1,1.0] -> 20.0
    assert factors["vwap_position"] == pytest.approx(20.0, abs=0.1)
    # 4. rsi_quality: 55 in [50,60] -> 20.0
    assert factors["rsi_quality"] == 20.0
    # 5. volume: 2000/1000=2.0 ratio >= 1.5 -> 20.0
    assert factors["volume"] == 20.0
    # 6. rr_ratio: reward/risk = 5.5/2.5 = 2.2, in [2.0,2.5) -> 15.0
    assert factors["rr_ratio"] == 15.0

    assert score == pytest.approx(sum(factors.values()))


def test_score_long_below_vwap_scores_zero_vwap_factor():
    strat = make_strategy()
    strat._orb_high = 100.0
    _, factors = strat._score_long(close=100.5, ema_f=10.1, ema_s=10.0, rsi_val=55,
                                    vwap_v=101.0,  # price below VWAP -> bad for long
                                    cur_vol=2000, avg_vol=1000, sl=98, target=106)
    assert factors["vwap_position"] == 0.0


def test_score_long_bad_rr_scores_zero():
    strat = make_strategy()
    strat._orb_high = 100.0
    _, factors = strat._score_long(close=100.5, ema_f=10.1, ema_s=10.0, rsi_val=55,
                                    vwap_v=100.2, cur_vol=2000, avg_vol=1000,
                                    sl=99.5, target=101.0)   # risk=1, reward=0.5 -> rr=0.5
    assert factors["rr_ratio"] == 0.0


def test_score_short_mirrors_long_logic():
    strat = make_strategy()
    strat._orb_low = 100.0

    close, ema_f, ema_s, rsi_val, vwap_v = 99.5, 9.9, 10.0, 45.0, 99.8
    cur_vol, avg_vol = 2000.0, 1000.0
    sl, target = 102.0, 94.0   # risk=2.5, reward=5.5, rr=2.2

    score, factors = strat._score_short(close, ema_f, ema_s, rsi_val, vwap_v,
                                         cur_vol, avg_vol, sl, target)
    assert factors["rsi_quality"] == 20.0     # 45 in [40,50]
    assert factors["volume"] == 20.0
    assert factors["rr_ratio"] == 15.0
    assert score == pytest.approx(sum(factors.values()))


# ----------------------------------------------------------------------
# Breakeven + ATR trailing stop (_check_exits)
# ----------------------------------------------------------------------

def _open_long(strat, entry=100.0, sl=95.0, target=115.0):
    strat._position       = "long"
    strat._entry_price    = entry
    strat._stop_loss      = sl
    strat._target         = target
    strat._initial_risk   = abs(entry - sl)
    strat._breakeven_done = False
    strat._candles_held   = 0


def test_breakeven_not_triggered_before_threshold():
    strat = make_strategy(breakeven_r=1.0, trail_atr_mult=1.0)
    _open_long(strat, entry=100, sl=95, target=115)  # risk=5

    sig = strat._check_exits(close=104, ema_f=10, ema_s=9, atr_val=2)  # profit=4 < 5
    assert sig.signal == Signal.HOLD
    assert strat._breakeven_done is False
    assert strat._stop_loss == 95  # unchanged


def test_breakeven_triggers_and_moves_stop_to_entry():
    strat = make_strategy(breakeven_r=1.0, trail_atr_mult=1.0)
    _open_long(strat, entry=100, sl=95, target=115)  # risk=5

    sig = strat._check_exits(close=105, ema_f=10, ema_s=9, atr_val=2)  # profit=5 >= 5
    assert sig.signal == Signal.HOLD
    assert strat._breakeven_done is True
    # breakeven raises stop to entry (100), then ATR trail immediately extends
    # it further to close - atr*mult = 105 - 2 = 103
    assert strat._stop_loss == pytest.approx(103.0)


def test_trailing_stop_ratchets_and_never_loosens_on_pullback():
    strat = make_strategy(breakeven_r=1.0, trail_atr_mult=1.0)
    _open_long(strat, entry=100, sl=95, target=115)

    strat._check_exits(close=105, ema_f=10, ema_s=9, atr_val=2)   # stop -> 103
    assert strat._stop_loss == pytest.approx(103.0)

    # Price pulls back to 102 -- naive trail (102-2=100) is BELOW the current
    # stop (103), so the stop must stay at 103, not loosen to 100.
    sig = strat._check_exits(close=102, ema_f=10, ema_s=9, atr_val=2)
    assert strat._stop_loss == pytest.approx(103.0)
    # And since close(102) <= stop_loss(103), this pullback must trigger the stop.
    assert sig.signal == Signal.EXIT_LONG
    assert "Stop hit" in sig.reason


def test_trailing_stop_extends_further_as_price_keeps_rising():
    strat = make_strategy(breakeven_r=1.0, trail_atr_mult=1.0)
    _open_long(strat, entry=100, sl=95, target=115)

    strat._check_exits(close=105, ema_f=10, ema_s=9, atr_val=2)  # stop -> 103
    strat._check_exits(close=110, ema_f=10, ema_s=9, atr_val=2)  # trail -> 110-2=108
    assert strat._stop_loss == pytest.approx(108.0)


def test_short_breakeven_and_trailing_mirror_long():
    strat = make_strategy(breakeven_r=1.0, trail_atr_mult=1.0)
    strat._position       = "short"
    strat._entry_price    = 100.0
    strat._stop_loss      = 105.0
    strat._target         = 85.0
    strat._initial_risk   = 5.0
    strat._breakeven_done = False
    strat._candles_held   = 0

    sig = strat._check_exits(close=95, ema_f=9, ema_s=10, atr_val=2)  # profit=5 >= 5
    assert strat._breakeven_done is True
    # breakeven -> min(105,100)=100; trail -> close+atr*mult=95+2=97 but min(100,97)=97
    assert strat._stop_loss == pytest.approx(97.0)


# ----------------------------------------------------------------------
# Time-based (max holding period) exit
# ----------------------------------------------------------------------

def test_max_holding_candles_forces_exit_at_exact_count():
    strat = make_strategy(max_holding_candles=3, breakeven_r=100.0, trail_atr_mult=100.0)
    _open_long(strat, entry=100, sl=90, target=200)  # unreachable SL/target

    sig1 = strat._check_exits(close=101, ema_f=10, ema_s=9, atr_val=1)  # held=1
    sig2 = strat._check_exits(close=101, ema_f=10, ema_s=9, atr_val=1)  # held=2
    sig3 = strat._check_exits(close=101, ema_f=10, ema_s=9, atr_val=1)  # held=3 -> exit

    assert sig1.signal == Signal.HOLD
    assert sig2.signal == Signal.HOLD
    assert sig3.signal == Signal.EXIT_LONG
    assert "Max holding period" in sig3.reason


def test_max_holding_candles_disabled_by_default_zero():
    strat = make_strategy(max_holding_candles=0, breakeven_r=100.0, trail_atr_mult=100.0)
    _open_long(strat, entry=100, sl=90, target=200)
    for _ in range(50):
        sig = strat._check_exits(close=101, ema_f=10, ema_s=9, atr_val=1)
        assert sig.signal == Signal.HOLD


# ----------------------------------------------------------------------
# EMA crossover exit still works independent of new features
# ----------------------------------------------------------------------

def test_ema_crossover_exit_when_no_stop_target_or_time_hit():
    strat = make_strategy(breakeven_r=100.0, trail_atr_mult=100.0, max_holding_candles=0)
    _open_long(strat, entry=100, sl=90, target=200)
    sig = strat._check_exits(close=101, ema_f=9, ema_s=10, atr_val=1)  # fast < slow
    assert sig.signal == Signal.EXIT_LONG
    assert "EMA bearish crossover" in sig.reason


# ----------------------------------------------------------------------
# force_exit() / reset() state hygiene
# ----------------------------------------------------------------------

def test_force_exit_resets_all_position_state():
    strat = make_strategy()
    _open_long(strat, entry=100, sl=95, target=115)
    strat._breakeven_done = True
    strat._candles_held   = 7

    strat.force_exit()

    assert strat._position is None
    assert strat._entry_price == 0.0
    assert strat._stop_loss == 0.0
    assert strat._target == 0.0
    assert strat._initial_risk == 0.0
    assert strat._breakeven_done is False
    assert strat._candles_held == 0


def test_reset_clears_orb_and_position_state():
    strat = make_strategy()
    _open_long(strat, entry=100, sl=95, target=115)
    strat._orb_high, strat._orb_low = 105, 95

    strat.reset()

    assert strat._position is None
    assert strat._orb_high is None
    assert strat._orb_low is None


# ----------------------------------------------------------------------
# Volatility gate — integration-level (full on_candle_close pipeline)
# ----------------------------------------------------------------------

def _make_breakout_history():
    """9 warm-up bars (mild uptrend chop) + 1 clear breakout bar, minimal
    period settings so `min_bars` is satisfied with a small dataset."""
    rows = []
    t0 = datetime(2024, 2, 1, 9, 15)
    closes = [100.0, 100.3, 100.1, 100.4, 100.3, 100.6, 100.5, 100.8, 101.0]
    for i, c in enumerate(closes):
        rows.append({"timestamp": t0 + timedelta(minutes=i), "open": c - 0.1,
                     "high": c + 0.15, "low": c - 0.15, "close": c, "volume": 1000 + i * 10})
    # breakout bar: clears ORB high (set from bar 0 = 100), big volume
    rows.append({"timestamp": t0 + timedelta(minutes=9), "open": 101.0, "high": 103.5,
                 "low": 100.9, "close": 103.0, "volume": 4000})
    return pd.DataFrame(rows)


def test_volatility_gate_blocks_when_atr_pct_outside_bounds():
    history = _make_breakout_history()
    strat = make_strategy(
        candle_minutes=1, orb_minutes=1, ema_fast=2, ema_slow=4,
        rsi_period=4, rsi_overbought=99.5, rsi_oversold=5, atr_period=4,
        vwap_filter=False, min_confidence=0.0, volume_avg_periods=4,
        breakeven_r=100.0, trail_atr_mult=100.0, max_holding_candles=0,
        max_atr_pct=0.001,   # unrealistically tight -> must block every setup
    )
    sig = strat.on_candle_close(history.iloc[-1], history)
    assert sig.signal == Signal.HOLD


def test_volatility_gate_allows_when_disabled():
    history = _make_breakout_history()
    strat = make_strategy(
        candle_minutes=1, orb_minutes=1, ema_fast=2, ema_slow=4,
        rsi_period=4, rsi_overbought=99.5, rsi_oversold=5, atr_period=4,
        vwap_filter=False, min_confidence=0.0, volume_avg_periods=4,
        breakeven_r=100.0, trail_atr_mult=100.0, max_holding_candles=0,
        min_atr_pct=0.0, max_atr_pct=100.0,
    )
    sig = strat.on_candle_close(history.iloc[-1], history)
    assert sig.signal == Signal.BUY
    assert 0.0 <= sig.confidence <= 100.0
