"""
Tests for backtest/options_bt.py — focus on the DEFINED-RISK invariant, which is
the whole point of using an iron condor/fly (bounded max loss) vs naked selling.
Uses the cached NIFTY/VIX data if present; skips gracefully if not.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from backtest.options_bt import run_iron_fly, IronFlyConfig, _IDX_DIR

_HAVE_DATA = (_IDX_DIR / "nifty_1d.csv").exists() and (_IDX_DIR / "vix_1d.csv").exists()
pytestmark = pytest.mark.skipif(not _HAVE_DATA, reason="cached NIFTY/VIX index data not present")


def test_defined_risk_loss_is_bounded():
    # For a defined-risk iron condor, no week's loss may exceed the wing width
    # (plus a small cost allowance). This is the safety invariant.
    cfg = IronFlyConfig(use_wings=True, short_offset_pct=1.5, wing_pct=1.0)
    r = run_iron_fly(cfg)
    assert r.n > 50
    for w in r.weeks:
        # max loss ~ wing width W (points) + costs; allow generous cost buffer
        assert w["pnl_pts"] >= -(w["W"] + 20), f"loss exceeded defined risk: {w}"


def test_max_profit_not_exceeding_credit():
    cfg = IronFlyConfig(use_wings=True, short_offset_pct=1.5, wing_pct=1.0)
    r = run_iron_fly(cfg)
    for w in r.weeks:
        # profit can't exceed the credit collected (points), minus nothing
        assert w["pnl_pts"] <= w["credit_pts"] + 1e-6


def test_naked_has_much_larger_margin_than_condor():
    naked = run_iron_fly(IronFlyConfig(use_wings=False))
    condor = run_iron_fly(IronFlyConfig(use_wings=True, short_offset_pct=1.5, wing_pct=1.0))
    assert naked.avg_margin_rs > condor.avg_margin_rs * 3  # naked needs far more margin


def test_result_metrics_populated():
    r = run_iron_fly(IronFlyConfig(short_offset_pct=2.0, wing_pct=1.0))
    assert r.n > 0
    assert isinstance(r.win_rate, float)
    assert isinstance(r.total_rs, float)
    assert r.max_drawdown_pts <= 0
