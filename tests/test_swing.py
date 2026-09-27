"""
Unit tests for strategy/swing.DailySwingStrategy and the delivery cost model.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from strategy.swing import DailySwingStrategy
from strategy.strategy_base import Signal
from execution.position_manager import zerodha_delivery_costs, zerodha_intraday_costs


def _daily(closes, start="2024-01-01"):
    ts = pd.date_range(start, periods=len(closes), freq="1D")
    closes = pd.Series(closes, dtype=float)
    highs = closes + 1.0
    lows = closes - 1.0
    return pd.DataFrame({"timestamp": ts, "open": closes, "high": highs,
                         "low": lows, "close": closes, "volume": 100000})


def _mk(**over):
    kw = dict(symbol="T-EQ", token="1", trend_sma=20, exit_sma=5, rsi_period=2,
              rsi_entry=15.0, rsi_exit=70.0, atr_period=5, atr_stop_mult=3.0,
              max_holding_days=10, allow_short=False, min_price=1.0)
    kw.update(over)
    return DailySwingStrategy(**kw)


def test_long_entry_on_pullback_in_uptrend():
    # Steep uptrend so close stays > SMA even after a 2-day pullback that drives
    # RSI(2) low.
    closes = [100 + 3 * i for i in range(30)]     # steep uptrend to ~187
    closes += [184, 180]                           # 2 down days -> RSI(2) low, still > SMA10
    hist = _daily(closes)
    strat = _mk(trend_sma=10, rsi_entry=40.0)
    sig = strat.on_candle_close(hist.iloc[-1], hist)
    assert sig.signal == Signal.BUY
    assert strat.get_position_direction() == "long"
    assert sig.stop_loss < sig.entry_price
    assert strat.get_current_target() is None       # signal-based exit


def test_no_long_when_below_trend():
    closes = [100 - i * 0.5 for i in range(45)]   # downtrend, close < SMA
    hist = _daily(closes)
    strat = _mk()
    sig = strat.on_candle_close(hist.iloc[-1], hist)
    assert sig.signal == Signal.HOLD


def test_long_exit_on_rsi_recovery():
    strat = _mk()
    strat._position, strat._entry_price, strat._stop_loss, strat._held = "long", 130.0, 120.0, 1
    # close jumps above the 5-SMA and RSI recovers -> exit
    closes = [130 + i for i in range(40)]         # strong up so SMA5 < close, RSI high
    hist = _daily(closes)
    sig = strat.on_candle_close(hist.iloc[-1], hist)
    assert sig.signal == Signal.EXIT_LONG


def test_long_exit_on_atr_stop():
    strat = _mk()
    strat._position, strat._entry_price, strat._stop_loss, strat._held = "long", 130.0, 125.0, 1
    closes = [130 - i for i in range(40)]         # falling; last close well below stop
    hist = _daily(closes)
    sig = strat.on_candle_close(hist.iloc[-1], hist)
    assert sig.signal == Signal.EXIT_LONG
    assert "stop" in sig.reason.lower()


def test_short_disabled_by_default():
    closes = [200 - 3 * i for i in range(30)] + [116, 122]  # downtrend then bounce, RSI(2) high
    hist = _daily(closes)
    assert _mk(trend_sma=10, allow_short=False).on_candle_close(hist.iloc[-1], hist).signal == Signal.HOLD


def test_short_entry_when_enabled():
    # Steep downtrend so close stays < SMA even after a 2-day bounce that drives
    # RSI(2) high.
    closes = [200 - 5 * i for i in range(30)] + [60, 68]  # steep downtrend then 2 up days
    hist = _daily(closes)
    sig = _mk(trend_sma=10, rsi_entry=40.0, allow_short=True).on_candle_close(hist.iloc[-1], hist)
    assert sig.signal == Signal.SELL
    assert sig.stop_loss > sig.entry_price


def test_min_price_gate():
    closes = [10 + i * 0.01 for i in range(40)] + [10.3, 10.1, 9.9]
    hist = _daily(closes)
    assert _mk(min_price=50.0).on_candle_close(hist.iloc[-1], hist).signal == Signal.HOLD


def test_max_holding_days_forces_exit():
    strat = _mk(max_holding_days=3, atr_stop_mult=100.0)  # stop unreachable
    strat._position, strat._entry_price, strat._stop_loss, strat._held = "long", 130.0, 1.0, 0
    # flat prices below SMA-exit so no signal exit; only the holding cap fires
    closes = [130] * 40
    hist = _daily(closes)
    strat._held = 2
    sig = strat.on_candle_close(hist.iloc[-1], hist)  # -> held=3
    assert sig.signal == Signal.EXIT_LONG
    assert "Max holding" in sig.reason


# --- delivery cost model ---

def test_delivery_costs_positive_and_include_dp_and_double_stt():
    c = zerodha_delivery_costs(100_000, 105_000)
    assert c.total > 0
    # delivery STT is 0.1% on BOTH legs => ~0.001*(205000)=205
    assert c.stt == pytest.approx(205.0, abs=1.0)
    # DP flat charge folded into brokerage bucket
    assert c.brokerage >= 15.0


def test_delivery_vs_intraday_shapes_differ():
    intr = zerodha_intraday_costs(100_000, 100_000)
    deliv = zerodha_delivery_costs(100_000, 100_000)
    # Delivery STT (both legs) is much higher than intraday (sell-only)
    assert deliv.stt > intr.stt
