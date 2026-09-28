"""
tests/test_options_registry.py

Guards the config-driven options system: the registry exposes the expected
strategies, get_strategy resolves by name (and rejects unknowns), and the sizer
produces a self-consistent lot plan (margin/DD within budget, profit scales).
Uses the complete real-data window so it is deterministic and offline.
"""
import pandas as pd
import pytest

from options.registry import list_strategies, get_strategy, LOT_SIZE
from options.sizer import plan

_START = "2024-08-01"  # complete window in the cache


def test_registry_lists_expected():
    names = list_strategies()
    assert {"weekly", "daily", "hybrid"} <= set(names)


def test_unknown_strategy_raises():
    with pytest.raises(ValueError):
        get_strategy("does-not-exist")


def test_weekly_has_one_component_daily_one_hybrid_two():
    assert len(get_strategy("weekly", start=_START).components) == 1
    assert len(get_strategy("daily", start=_START).components) == 1
    assert len(get_strategy("hybrid", start=_START).components) == 2


def test_components_have_real_pnl_and_margin():
    w = get_strategy("weekly", start=_START)
    c = w.components[0]
    assert isinstance(c.per_lot_pnl, pd.Series)
    assert len(c.per_lot_pnl) > 0
    assert c.margin_per_lot > 0


def test_sizer_respects_budgets():
    strat = get_strategy("daily", start=_START)
    p = plan(strat, capital=200_000, dd_budget=0.25, margin_budget=0.60)
    assert sum(p.lots.values()) >= 1
    assert p.margin_used <= 0.60 * 200_000
    assert -p.max_drawdown <= 0.25 * 200_000 + 1  # within budget (float slack)
    assert p.annual_profit > 0


def test_sizer_too_small_returns_zero_lots():
    strat = get_strategy("weekly", start=_START)
    p = plan(strat, capital=20_000, dd_budget=0.25, margin_budget=0.60)
    assert sum(p.lots.values()) == 0


def test_more_capital_never_fewer_lots():
    strat = get_strategy("daily", start=_START)
    small = plan(strat, 100_000)
    big = plan(strat, 400_000)
    assert sum(big.lots.values()) >= sum(small.lots.values())


def test_lot_size_constant():
    assert LOT_SIZE == 75
