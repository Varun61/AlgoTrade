"""
options/sizer.py

Turns an OptionsStrategy + your capital into a concrete LOT PLAN and its expected
economics, all from the real-data P&L streams.

Sizing rule (honest, risk-first): choose the lot count per component that
MAXIMISES annual profit subject to
  - worst historical peak-to-trough drawdown <= dd_budget * capital  (default 25%)
  - total margin blocked                     <= margin_budget * capital (default 60%)
For the hybrid (2 components) it grid-searches the lot mix; for single-component
strategies it just scales.

Everything returned is grounded in the backtest window that built the strategy —
so if you pass a 5-year strategy you get 5-year drawdown, etc.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from options.registry import OptionsStrategy, Component


@dataclass
class LotPlan:
    lots: dict[str, int]              # component label -> lots
    capital: int
    margin_used: float
    annual_profit: float
    annual_return_pct: float
    max_drawdown: float               # ₹ (negative)
    max_drawdown_pct: float
    worst_month: float                # ₹ (negative)
    best_month: float
    median_month: float
    pct_positive_months: float
    combined_pnl: pd.Series = field(default_factory=pd.Series)

    @property
    def deployed_pct(self) -> float:
        return self.margin_used / self.capital * 100 if self.capital else 0.0


def _years(ser: pd.Series) -> float:
    if ser.empty:
        return 1.0
    span = (ser.index.max() - ser.index.min()).days / 365.25
    return max(span, 1 / 12)


def _combined(components: list[Component], lots: list[int]) -> pd.Series:
    out = pd.Series(dtype=float)
    for c, n in zip(components, lots):
        if n:
            out = (c.per_lot_pnl * n).add(out, fill_value=0)
    return out.sort_index()


def _drawdown(ser: pd.Series) -> float:
    if ser.empty:
        return 0.0
    eq = ser.cumsum()
    return float((eq - eq.cummax()).min())


def plan(strategy: OptionsStrategy, capital: int,
         dd_budget: float = 0.25, margin_budget: float = 0.60) -> LotPlan:
    comps = strategy.components
    max_lots = [max(1, int(margin_budget * capital / c.margin_per_lot) + 1) for c in comps]

    best = None  # (annual_profit, lots)
    if len(comps) == 1:
        candidates = ([n] for n in range(0, max_lots[0] + 1))
    else:  # 2-component grid (hybrid)
        candidates = ([a, b] for a in range(0, max_lots[0] + 1)
                      for b in range(0, max_lots[1] + 1))

    for lots in candidates:
        if sum(lots) == 0:
            continue
        margin = sum(c.margin_per_lot * n for c, n in zip(comps, lots))
        if margin > margin_budget * capital:
            continue
        comb = _combined(comps, lots)
        dd = _drawdown(comb)
        if -dd > dd_budget * capital:
            continue
        ann = comb.sum() / _years(comb)
        if best is None or ann > best[0]:
            best = (ann, list(lots), comb, dd, margin)

    if best is None:  # capital too small for even 1 lot within budgets
        return LotPlan(lots={c.label: 0 for c in comps}, capital=capital,
                       margin_used=0, annual_profit=0, annual_return_pct=0,
                       max_drawdown=0, max_drawdown_pct=0, worst_month=0,
                       best_month=0, median_month=0, pct_positive_months=0,
                       combined_pnl=pd.Series(dtype=float))

    ann, lots, comb, dd, margin = best
    m = comb.groupby(comb.index.to_period("M")).sum()
    full = pd.period_range(comb.index.min().to_period("M"),
                           comb.index.max().to_period("M"), freq="M")
    m = m.reindex(full, fill_value=0)
    return LotPlan(
        lots={c.label: n for c, n in zip(comps, lots)},
        capital=capital, margin_used=margin,
        annual_profit=ann, annual_return_pct=ann / capital * 100,
        max_drawdown=dd, max_drawdown_pct=dd / capital * 100,
        worst_month=float(m.min()), best_month=float(m.max()),
        median_month=float(m.median()),
        pct_positive_months=float((m > 0).mean() * 100),
        combined_pnl=comb,
    )
