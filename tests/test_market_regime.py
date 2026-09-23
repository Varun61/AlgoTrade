import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from risk.market_regime import compute_market_regime


def _trending_nifty(n=60, start=20000.0, daily_move=60.0):
    """Steadily trending daily candles -> high ADX."""
    closes = start + np.arange(n) * daily_move
    highs = closes + 30
    lows = closes - 30
    return pd.DataFrame({"high": highs, "low": lows, "close": closes})


def _choppy_nifty(n=60, start=20000.0, amplitude=80.0):
    """Oscillating sideways daily candles -> low ADX."""
    closes = start + amplitude * np.sin(np.arange(n) * 0.9)
    highs = closes + 30
    lows = closes - 30
    return pd.DataFrame({"high": highs, "low": lows, "close": closes})


def test_trending_market_low_vix_is_favorable():
    ok, reason = compute_market_regime(_trending_nifty(), vix_value=13.0)
    assert ok is True
    assert "favorable" in reason.lower()


def test_choppy_market_is_unfavorable():
    ok, reason = compute_market_regime(_choppy_nifty(), vix_value=13.0)
    assert ok is False
    assert "choppy" in reason.lower()


def test_high_vix_is_unfavorable_even_if_trending():
    ok, reason = compute_market_regime(_trending_nifty(), vix_value=25.0)
    assert ok is False
    assert "vix" in reason.lower()


def test_missing_vix_falls_back_to_adx_only():
    ok, reason = compute_market_regime(_trending_nifty(), vix_value=None)
    assert ok is True


def test_insufficient_history_defaults_to_allow():
    short_df = _trending_nifty(n=5)
    ok, reason = compute_market_regime(short_df, vix_value=13.0)
    assert ok is True
    assert "insufficient" in reason.lower()


def test_none_history_defaults_to_allow():
    ok, reason = compute_market_regime(None, vix_value=13.0)
    assert ok is True
