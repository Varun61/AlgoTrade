"""
tools/capital_sizing.py

Turns the REAL-DATA backtest per-lot economics into a capital-tier profit table,
comparing DAILY (0DTE iron-fly) vs WEEKLY (VIX-timed iron condor).

Sizing rule (risk-controlled, honest):
  lots = min(
     floor(0.35 * capital / worst_single_loss_per_lot),   # a worst event ~<=35% of capital
     floor(0.75 * capital / margin_per_lot),               # margin <=75% of capital (buffer)
  )
NIFTY lot = 75. Numbers come straight from backtest/options_*_bt.py on real NSE
prices (Aug-2024 .. Sep-2026, ~2.15 yr). Backtest, not live — see caveats.

    python -m tools.capital_sizing
"""
from __future__ import annotations
import sys
from datetime import date
from math import floor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
from backtest.options_multi_bt import backtest, iron_condor
from backtest.options_intraday_bt import run_intraday

YRS = (date(2026, 9, 25) - date(2024, 8, 1)).days / 365.25
TIERS = [25_000, 50_000, 100_000, 200_000, 500_000, 1_000_000]


def per_lot(kind: str):
    if kind == "weekly":
        r = backtest(lambda s, rg: (("c", iron_condor(s, 3.0, 2.0)) if rg.vix_pct >= 40 else None))
        margin = np.percentile([x["margin_rs"] for x in r.weeks if x["strat"] != "flat"], 90)
        worst, maxdd, yr, n = r.worst_rs, r.max_dd, r.total_rs / YRS, r.n_traded
        pf, sh = r.profit_factor, r.sharpe
    else:  # daily 0DTE
        r = run_intraday("straddle", use_wings=True, wing_pct=1.0, vix_min=40, slip_pts_per_leg=2)
        margin = np.percentile([x["margin_rs"] for x in r.days], 90)
        worst, maxdd, yr, n = r.worst_rs, r.max_dd, r.total_rs / YRS, r.n
        pf, sh = r.profit_factor, r.sharpe
    return {"yr": yr, "worst": abs(worst), "maxdd": abs(maxdd),
            "margin": margin, "n_yr": n / YRS, "pf": pf, "sh": sh}


def lots_for(cap, pl):
    by_risk = floor(0.35 * cap / pl["worst"])
    by_margin = floor(0.75 * cap / pl["margin"])
    return max(0, min(by_risk, by_margin))


def table(name, pl):
    print(f"\n=== {name} ===")
    print(f"per-lot: ₹{pl['yr']:,.0f}/yr | worst event ₹{pl['worst']:,.0f} | maxDD ₹{pl['maxdd']:,.0f} "
          f"| margin ₹{pl['margin']:,.0f} | ~{pl['n_yr']:.0f} trades/yr | PF {pl['pf']} | Sharpe {pl['sh']}")
    print(f"{'capital':>10} {'lots':>5} {'profit/yr':>11} {'return%':>8} {'worstDD':>11} {'worstDD%':>9}")
    for cap in TIERS:
        n = lots_for(cap, pl)
        if n == 0:
            print(f"{cap:>10,} {0:>5} {'— too small —':>11} {'':>8} {'':>11} {'':>9}")
            continue
        profit = n * pl["yr"]
        worst = n * pl["maxdd"]
        print(f"{cap:>10,} {n:>5} {profit:>11,.0f} {profit/cap*100:>7.1f}% "
              f"{-worst:>11,.0f} {worst/cap*100:>8.1f}%")


def main():
    daily = per_lot("daily")
    weekly = per_lot("weekly")
    table("DAILY — 0DTE iron-fly ATM (VIX>=40, ~2pt/leg slippage)", daily)
    table("WEEKLY — VIX-timed iron condor 3%/2% (hold to expiry)", weekly)
    print("\nCaveats: real NSE prices but only ~2.15yr (mostly calm/bull, no crash in "
          "sample); 0DTE assumes ~2pt/leg fills; backtest not yet live-verified.")


if __name__ == "__main__":
    main()
