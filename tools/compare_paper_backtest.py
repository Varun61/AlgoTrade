"""
tools/compare_paper_backtest.py

Out-of-sample validation: does the LIVE paper trading match the BACKTEST?

Reads the paper logs (logs/options_paper.jsonl and options_paper_em.jsonl), pairs
each OPEN with its SETTLE, and reports realized stats (trades, win%, PF, avg P&L,
credit) SIDE BY SIDE with the backtest's expected distribution for that same
strategy. This is how we decide whether the edge survives real fills before
committing real money — especially for the experimental expected-move variant.

    python -m tools.compare_paper_backtest
"""
from __future__ import annotations
import json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
from options.registry import get_strategy

_LOG_DIR = Path(__file__).parent.parent / "logs"


def _paper_trades(path: Path) -> list[dict]:
    """Pair OPEN+SETTLE records into completed paper trades."""
    if not path.exists():
        return []
    opens, trades = {}, []
    for line in open(path):
        r = json.loads(line)
        if r.get("event") == "OPEN":
            opens[r["expiry"]] = r
        elif r.get("event") == "SETTLE" and r["expiry"] in opens:
            o = opens[r["expiry"]]
            trades.append({"expiry": r["expiry"], "credit_pts": o.get("credit_pts"),
                           "pnl_rs": r.get("pnl_rs")})
    return trades


def _dist(pnls: list[float]) -> dict:
    p = np.array(pnls, float)
    if len(p) == 0:
        return {}
    wins = p[p > 0]; losses = p[p < 0]
    return {
        "n": len(p), "win%": round((p > 0).mean() * 100, 1),
        "PF": round(wins.sum() / -losses.sum(), 2) if len(losses) else float("inf"),
        "total": round(p.sum()), "avg": round(p.mean()),
        "best": round(p.max()), "worst": round(p.min()),
    }


def _backtest_ref(name: str) -> dict:
    """Expected per-lot P&L distribution from the historical backtest."""
    strat = get_strategy(name)
    ser = strat.components[0].per_lot_pnl
    return _dist(ser.tolist())


def _report(name: str, log_path: Path):
    print(f"\n{'='*60}\n {name}   (log: {log_path.name})\n{'='*60}")
    trades = _paper_trades(log_path)
    ref = _backtest_ref(name)
    paper = _dist([t["pnl_rs"] for t in trades if t["pnl_rs"] is not None])

    def row(label, d):
        if not d:
            print(f"  {label:<10} (no data)"); return
        print(f"  {label:<10} n={d['n']:<3} win%={d['win%']:<5} PF={d['PF']:<5} "
              f"avg=₹{d['avg']:<7,} worst=₹{d['worst']:,}")
    row("BACKTEST", ref)
    row("PAPER", paper)

    if not trades:
        print("  → No settled paper trades yet. Once a few high-VIX weeks complete,\n"
              "    rerun this to see if live win%/PF/avg track the backtest above.")
        return
    print("  per-trade (paper):")
    for t in trades:
        c = f"{t['credit_pts']:.1f}pts" if t["credit_pts"] is not None else "?"
        print(f"    {t['expiry']}  credit {c:<10} P&L ₹{t['pnl_rs']:,.0f}")
    # simple verdict
    if paper and ref:
        wr_gap = paper["win%"] - ref["win%"]
        verdict = ("tracking backtest" if abs(wr_gap) <= 15 and paper["PF"] >= 1.2
                   else "DIVERGING from backtest — investigate before scaling")
        print(f"  → win% gap {wr_gap:+.0f} vs backtest; PF {paper['PF']} → {verdict}")


def main():
    print("PAPER vs BACKTEST — out-of-sample validation")
    _report("weekly", _LOG_DIR / "options_paper.jsonl")
    _report("weekly_em", _LOG_DIR / "options_paper_em.jsonl")
    print("\nNote: needs several settled weeks to be meaningful (profit is lumpy — "
          "75% of it comes from ~10% of weeks). Judge over a full cycle, not 2-3 trades.")


if __name__ == "__main__":
    main()
