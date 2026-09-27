"""
tools/analyze_confidence_factors.py

Validates each individual confidence sub-factor (orb_breakout, ema_trend,
vwap_position, rsi_quality, volume) against real trade outcomes, the same
way rr_ratio was proven to be constant/useless and removed. The aggregate
`confidence` score has already been shown to be non-predictive (and possibly
inverted) — this checks whether that's true of ALL sub-factors, or just some,
so we know what to fix vs. what to keep as-is.

Requires trades.csv to have per-factor columns (factor_orb_breakout,
factor_ema_trend, etc.) — only present in backtest runs AFTER the
confidence_factors logging change in backtest/engine.py.

Usage:
    python -m tools.analyze_confidence_factors backtest_results_full
"""
import sys
from pathlib import Path

import pandas as pd


def main():
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "backtest_results_full")
    trades = pd.read_csv(out_dir / "trades.csv")

    factor_cols = [c for c in trades.columns if c.startswith("factor_")]
    if not factor_cols:
        print(f"No factor_* columns found in {out_dir}/trades.csv — rerun the backtest "
              f"after the confidence_factors logging change in backtest/engine.py.")
        return

    print(f"\n=== {out_dir} : {len(trades)} trades, factors={factor_cols} ===\n")

    for col in factor_cols:
        name = col.replace("factor_", "")
        vals = trades[col]
        print(f"--- {name} (range {vals.min():.1f}-{vals.max():.1f}) ---")

        # Correlation with win (point-biserial) and pnl — quick predictiveness signal.
        win_corr = vals.corr(trades["win"].astype(float))
        pnl_corr = vals.corr(trades["pnl"])
        print(f"  corr(factor, win)={win_corr:+.3f}  corr(factor, pnl)={pnl_corr:+.3f}")

        # Bucket win-rate/P&L breakdown (quartiles, or unique values if few distinct levels).
        n_unique = vals.nunique()
        if n_unique <= 6:
            bucket = vals
        else:
            bucket = pd.qcut(vals, q=4, duplicates="drop")
        g = trades.groupby(bucket, observed=True).agg(
            trades=("pnl", "count"), win_rate=("win", "mean"), pnl=("pnl", "sum")
        )
        g["win_rate"] = (g["win_rate"] * 100).round(1)
        print(g, "\n")


if __name__ == "__main__":
    main()
