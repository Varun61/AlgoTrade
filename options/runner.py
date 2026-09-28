"""
options/runner.py

Reads config/settings.yaml `options:` block, builds the SELECTED strategy by
name, sizes it to your capital, and prints the expected economics (profit,
drawdown, margin used, capital deployed, monthly distribution).

    python -m options.runner                      # uses settings.yaml
    python -m options.runner --strategy hybrid --capital 300000
    python -m options.runner --compare            # show all strategies side by side

Data note: by default it backtests on the COMPLETE real window in the cache. Use
--start/--end to restrict (e.g. once 5yr history finishes downloading).
"""
from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from options.registry import get_strategy, list_strategies
from options.sizer import plan, LotPlan

_SETTINGS = Path(__file__).parent.parent / "config" / "settings.yaml"


def _cfg() -> dict:
    with open(_SETTINGS) as f:
        return (yaml.safe_load(f) or {}).get("options", {})


def _print_plan(name: str, strat, p: LotPlan):
    print(f"\n=== {name.upper()} — {strat.description} ===")
    print(f"min capital: ₹{strat.min_capital:,} | your capital: ₹{p.capital:,}")
    if sum(p.lots.values()) == 0:
        print("  ⚠️  capital too small to run even 1 lot within the risk/margin budget.")
        print(f"     (needs ~₹{strat.margin_one_of_each:,.0f} margin for one unit)")
        return
    lots_str = ", ".join(f"{n}× {lbl}" for lbl, n in p.lots.items() if n)
    print(f"  lots            : {lots_str}")
    print(f"  margin used     : ₹{p.margin_used:,.0f}  ({p.deployed_pct:.0f}% of capital deployed, "
          f"₹{p.capital - p.margin_used:,.0f} kept free)")
    print(f"  expected profit : ₹{p.annual_profit:,.0f}/yr  ({p.annual_return_pct:.0f}% / yr)")
    print(f"  worst drawdown  : ₹{p.max_drawdown:,.0f}  ({p.max_drawdown_pct:.0f}% of capital)")
    print(f"  monthly P&L     : median ₹{p.median_month:,.0f} | best ₹{p.best_month:,.0f} | "
          f"worst ₹{p.worst_month:,.0f} | {p.pct_positive_months:.0f}% months positive")


def main():
    cfg = _cfg()
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default=cfg.get("strategy", "weekly"))
    ap.add_argument("--capital", type=int, default=int(cfg.get("capital", 100000)))
    ap.add_argument("--dd", type=float, default=float(cfg.get("max_drawdown_pct", 25)) / 100)
    ap.add_argument("--margin", type=float, default=float(cfg.get("max_margin_pct", 60)) / 100)
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--compare", action="store_true", help="show all strategies")
    args = ap.parse_args()

    names = list_strategies() if args.compare else [args.strategy]
    for name in names:
        strat = get_strategy(name, start=args.start, end=args.end)
        p = plan(strat, args.capital, dd_budget=args.dd, margin_budget=args.margin)
        _print_plan(name, strat, p)
    print("\n(Real NSE prices; backtest not yet live-verified. Live trading gated.)")


if __name__ == "__main__":
    main()
