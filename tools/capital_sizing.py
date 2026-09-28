"""
tools/capital_sizing.py

Turns the REAL-DATA backtest per-lot economics into a capital-tier profit table,
comparing DAILY (0DTE iron-fly), WEEKLY (VIX-timed iron condor), and a HYBRID of
the two. All P&L is per YEAR, from real NSE prices (Aug-2024..Sep-2026, ~2.15 yr).

Why a hybrid helps: the weekly and daily P&L streams are only ~0.18 correlated,
so blending them earns more return per unit of drawdown than either alone.

Sizing rule (risk-controlled, honest): pick the lot mix that MAXIMISES annual
profit subject to
  - worst HISTORICAL drawdown <= dd_budget * capital   (default 35%)
  - total margin              <= margin_budget * capital (default 75%)
NIFTY lot = 75. Numbers straight from backtest/options_*_bt.py.

    python -m tools.capital_sizing
"""
from __future__ import annotations
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
from backtest.options_multi_bt import backtest, iron_condor
from backtest.options_intraday_bt import run_intraday

YRS = (date(2026, 9, 25) - date(2024, 8, 1)).days / 365.25
TIERS = [25_000, 50_000, 100_000, 200_000, 500_000, 1_000_000]
MARGIN_WEEKLY = 37_000
MARGIN_DAILY = 11_600
DD_BUDGET = 0.35
MARGIN_BUDGET = 0.75


def _series():
    w = backtest(lambda s, rg: (("c", iron_condor(s, 3.0, 2.0)) if rg.vix_pct >= 40 else None))
    wser = pd.Series({pd.Timestamp(x["expiry"]): x["pnl_rs"]
                      for x in w.weeks if x["strat"] != "flat"}).sort_index()
    d = run_intraday("straddle", use_wings=True, wing_pct=1.0, vix_min=40, slip_pts_per_leg=2)
    dser = pd.Series({pd.Timestamp(x["date"]): x["pnl_rs"] for x in d.days}).sort_index()
    return wser, dser


def _combo(wser, dser, lw, ld):
    comb = (wser * lw).add(dser * ld, fill_value=0).sort_index()
    eq = comb.cumsum()
    dd = float((eq - eq.cummax()).min())
    return comb.sum() / YRS, dd  # annual ₹, maxDD ₹ (<=0)


def _best(wser, dser, cap, wonly=False, donly=False):
    best = None
    for lw in range(0, 26):
        if donly and lw > 0:
            break
        for ld in range(0, 60):
            if wonly and ld > 0:
                break
            if lw == 0 and ld == 0:
                continue
            if lw * MARGIN_WEEKLY + ld * MARGIN_DAILY > MARGIN_BUDGET * cap:
                continue
            ann, dd = _combo(wser, dser, lw, ld)
            if -dd > DD_BUDGET * cap:
                continue
            if best is None or ann > best[0]:
                best = (ann, dd, lw, ld)
    return best


def _fmt(x, cap):
    if not x:
        return "— too small —"
    ann, dd, lw, ld = x
    return f"₹{ann:>9,.0f}/yr  {ann/cap*100:>4.0f}%  DD {-dd/cap*100:>3.0f}%  [{lw}wk+{ld}dy]"


def main():
    wser, dser = _series()
    corr = (wser.groupby(wser.index.to_period("M")).sum()
            .corr(dser.groupby(dser.index.to_period("M")).sum()))
    print(f"weekly/daily monthly-P&L correlation: {corr:.2f}  (low => hybrid diversifies)")
    print(f"per-lot/yr: weekly ₹{wser.sum()/YRS:,.0f} (margin ₹{MARGIN_WEEKLY:,}), "
          f"daily ₹{dser.sum()/YRS:,.0f} (margin ₹{MARGIN_DAILY:,})")
    print(f"\nSized so worst historical drawdown <= {DD_BUDGET:.0%} of capital, "
          f"margin <= {MARGIN_BUDGET:.0%}:\n")
    print(f"{'capital':>10} | {'HYBRID (best)':<38} | {'WEEKLY only':<30} | {'DAILY only':<30}")
    print("-" * 116)
    for cap in TIERS:
        h = _best(wser, dser, cap)
        wo = _best(wser, dser, cap, wonly=True)
        do = _best(wser, dser, cap, donly=True)
        print(f"{cap:>10,} | {_fmt(h, cap):<38} | {_fmt(wo, cap):<30} | {_fmt(do, cap):<30}")
    print("\nCaveats: real prices but only ~2.15yr (mostly calm/bull, NO crash in sample); "
          "0DTE assumes ~2pt/leg fills; both are DEFINED-RISK so a crash is capped; "
          "backtest not yet live-verified. For real money consider a tighter DD budget (~20%).")


if __name__ == "__main__":
    main()
