"""
tools/analyze_tail.py

Answers "where does the tail come from, and can we see it at entry?" for the
weekly condor variants. For every backtest trade it records the entry-time signals
(VIX, VIX percentile, IV/RV, prior-5d return) and the subsequent weekly move, then:
  1. P&L bucketed by subsequent move (<1% / 1-2% / >2%) — the tail-source table
  2. entry-feature means for SAFE (<=2%) vs DANGER (>2%) weeks — is the tail
     predictable at entry? (if the means overlap, it is NOT).

    python -m tools.analyze_tail                 # weekly (fixed)
    python -m tools.analyze_tail --variant em    # expected-move
"""
from __future__ import annotations
import argparse, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
from backtest.options_multi_bt import backtest, iron_condor, em_condor
from options.registry import (WEEKLY_SHORT_PCT, WEEKLY_WING_PCT, WEEKLY_VIX_MIN_PCTL,
                              EM_IVRV_MIN, EM_SHORT_MULT, EM_WING_MULT)


def _run(variant: str):
    rows = []

    def router(sp, rg):
        if variant == "em":
            if rg.vix_pct >= WEEKLY_VIX_MIN_PCTL and rg.iv_rv >= EM_IVRV_MIN:
                rows.append((rg.vix, rg.vix_pct, rg.iv_rv, rg.ret5))
                return ("t", em_condor(sp, rg.vix, EM_SHORT_MULT, EM_WING_MULT))
            return None
        if rg.vix_pct >= WEEKLY_VIX_MIN_PCTL:
            rows.append((rg.vix, rg.vix_pct, rg.iv_rv, rg.ret5))
            return ("t", iron_condor(sp, WEEKLY_SHORT_PCT, WEEKLY_WING_PCT))
        return None

    r = backtest(router, start="2021-07-16")
    tr = [x for x in r.weeks if x["strat"] != "flat"]
    df = pd.DataFrame(tr)
    f = pd.DataFrame(rows[:len(df)], columns=["vix", "vpct", "ivrv", "ret5"])
    df = pd.concat([df.reset_index(drop=True), f], axis=1)
    df["move"] = (df["S_exp"] - df["S0"]).abs() / df["S0"] * 100
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default="fixed", choices=["fixed", "em"])
    args = ap.parse_args()
    df = _run(args.variant)
    print(f"=== {args.variant} condor — tail-source (by subsequent weekly move) ===")
    for lo, hi, lab in [(0, 1, "<1%"), (1, 2, "1-2%"), (2, 100, ">2%")]:
        m = (df.move >= lo) & (df.move < hi); p = df.pnl_rs[m].values
        if len(p):
            pf = p[p > 0].sum() / max(1, -p[p < 0].sum())
            print(f"  {lab:<6} n={len(p):<3} PF={pf:<7.2f} avg=₹{p.mean():>7,.0f} worst=₹{p.min():>9,.0f}")
    print("\n=== is the tail predictable AT ENTRY? (safe<=2% vs danger>2%) ===")
    safe, danger = df[df.move <= 2], df[df.move > 2]
    for col, nm in [("vpct", "VIX percentile"), ("vix", "VIX level"),
                    ("ivrv", "IV/RV"), ("ret5", "prior 5d ret%")]:
        print(f"  {nm:<16} safe={safe[col].mean():>7.2f}   danger={danger[col].mean():>7.2f}")
    print(f"  n: safe={len(safe)} danger={len(danger)}")
    print("  → overlapping means = tail NOT predictable at entry (irreducible; the "
          "wider fixed IC survives it rather than predicting it).")


if __name__ == "__main__":
    main()
