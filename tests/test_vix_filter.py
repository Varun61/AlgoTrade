"""
tests/test_vix_filter.py

Guards the VIX-timing gate — the single most important rule of the live strategy
(only sell when premium is rich). If this logic breaks, the paper/live runner
would trade every week, which the 5yr backtest showed cuts returns ~4x and
triples the drawdown.
"""
from options.vix_filter import vix_percentile, should_sell
from options.registry import WEEKLY_VIX_MIN_PCTL


def test_percentile_latest_is_highest():
    # latest value is the max -> 100th percentile
    assert vix_percentile([10, 11, 12, 13, 20]) == 100.0


def test_percentile_latest_is_lowest():
    # latest is the min -> low percentile (1/5 = 20% since it counts itself)
    assert vix_percentile([20, 18, 16, 14, 10]) == 20.0


def test_percentile_needs_history():
    assert vix_percentile([]) is None
    assert vix_percentile([12]) is None


def test_percentile_uses_trailing_window():
    # only the last `window` values count
    series = [100] * 50 + [1, 2, 3, 4, 5]   # latest (5) is small vs the trailing window
    p = vix_percentile(series, window=5)     # window = [1,2,3,4,5] -> latest is max
    assert p == 100.0


def test_should_sell_gates_on_threshold():
    # latest is the max -> 100 pctl -> sell
    sell, pctl = should_sell([10, 12, 14, 16, 25], threshold=WEEKLY_VIX_MIN_PCTL)
    assert sell is True and pctl == 100.0
    # latest is the min -> 20 pctl -> do not sell
    sell, pctl = should_sell([25, 20, 16, 14, 10], threshold=WEEKLY_VIX_MIN_PCTL)
    assert sell is False


def test_should_sell_sits_out_without_history():
    # insufficient history -> conservatively sit out
    sell, pctl = should_sell([])
    assert sell is False and pctl is None



def test_load_series_rejects_short_history(tmp_path):
    """Guard: a truncated/corrupt cache must return None (fail loud), never a
    tiny series that would (a) skip forever or (b) get written back over good data."""
    import tools.run_options_paper as R
    import pandas as pd
    bad = tmp_path / "vix_bad.csv"
    pd.DataFrame({"timestamp": ["2026-09-29 00:00:00+0530"], "close": [13.6]}).to_csv(bad, index=False)
    assert R._load_series(bad) is None            # only 1 row (< _MIN_HISTORY)
    assert R._load_series(tmp_path / "missing.csv") is None  # missing file
