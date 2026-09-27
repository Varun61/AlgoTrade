"""
tools/analyze_backtest.py

Post-hoc breakdown of a completed portfolio backtest's trades.csv:
  1. Win rate / P&L by confidence-score bucket
  2. Realized avg-win vs avg-loss vs the configured ATR stop/target ratio
  3. Per-symbol P&L (worst/best offenders)
  4. Market-cap bucket P&L (Nifty100 / Midcap100 / Smallcap100 / Other),
     using data/index_lists/*.csv
  5. Rotation ("Rotated out...") exit-reason P&L in isolation

Usage:
    python tools/analyze_backtest.py backtest_results_full
"""
import sys
from pathlib import Path

import pandas as pd
import yaml


def load_cap_map() -> dict[str, str]:
    cap_map = {}
    files = {
        "Nifty100"     : "data/index_lists/nifty100.csv",
        "Midcap100"    : "data/index_lists/niftymidcap100.csv",
        "Smallcap100"  : "data/index_lists/niftysmallcap100.csv",
    }
    for bucket, path in files.items():
        p = Path(path)
        if not p.exists():
            continue
        df = pd.read_csv(p)
        for sym in df["Symbol"]:
            cap_map[f"{sym}-EQ"] = bucket
    return cap_map


def main():
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "backtest_results_full")
    trades = pd.read_csv(out_dir / "trades.csv")
    trades["reason_bucket"] = trades["reason"].str.extract(r"^([A-Za-z ]+)")[0].str.strip()

    print(f"\n=== {out_dir} : {len(trades)} trades ===\n")

    # 1. Confidence bucket breakdown
    if "confidence" in trades.columns:
        trades["conf_bucket"] = pd.cut(
            trades["confidence"], bins=[0, 60, 70, 80, 90, 100],
            labels=["<=60", "60-70", "70-80", "80-90", "90-100"]
        )
        print("--- Win rate / P&L by confidence bucket ---")
        g = trades.groupby("conf_bucket", observed=True).agg(
            trades=("pnl", "count"), win_rate=("win", "mean"), pnl=("pnl", "sum")
        )
        g["win_rate"] = (g["win_rate"] * 100).round(1)
        print(g, "\n")

    # 2. Realized avg win vs avg loss
    wins = trades[trades["pnl"] > 0]["pnl"]
    losses = trades[trades["pnl"] <= 0]["pnl"]
    avg_win = wins.mean() if len(wins) else 0
    avg_loss = losses.mean() if len(losses) else 0
    settings = yaml.safe_load(Path("config/settings.yaml").read_text())
    stop_mult = settings["risk"]["atr_stop_multiplier"]
    target_mult = settings["risk"]["atr_target_multiplier"]
    print("--- Realized R:R vs configured ATR ratio ---")
    print(f"Configured ATR stop:target = 1 : {target_mult / stop_mult:.2f}")
    print(f"Realized avg win: {avg_win:.2f} | avg loss: {avg_loss:.2f} | "
          f"realized ratio: 1 : {abs(avg_win / avg_loss):.2f}\n" if avg_loss else "")

    # 3. Per-symbol P&L (worst/best 15)
    print("--- Worst 15 symbols by P&L ---")
    sym_pnl = trades.groupby("symbol")["pnl"].agg(["sum", "count"]).sort_values("sum")
    print(sym_pnl.head(15), "\n")
    print("--- Best 15 symbols by P&L ---")
    print(sym_pnl.sort_values("sum", ascending=False).head(15), "\n")

    # 4. Market-cap bucket P&L
    cap_map = load_cap_map()
    if cap_map:
        trades["cap_bucket"] = trades["symbol"].map(cap_map).fillna("Other/Unclassified")
        print("--- P&L by market-cap bucket ---")
        g = trades.groupby("cap_bucket").agg(
            trades=("pnl", "count"), win_rate=("win", "mean"), pnl=("pnl", "sum")
        )
        g["win_rate"] = (g["win_rate"] * 100).round(1)
        print(g, "\n")
    else:
        print("(No index_lists found — skipping market-cap breakdown)\n")

    # 5. Rotation exit-reason isolation
    rotated = trades[trades["reason"].str.contains("Rotated", na=False)]
    print(f"--- Rotation exits: {len(rotated)} trades, P&L={rotated['pnl'].sum():.2f}, "
          f"win_rate={rotated['win'].mean() * 100:.1f}% ---" if len(rotated) else
          "--- No rotation exits found ---")


if __name__ == "__main__":
    main()
