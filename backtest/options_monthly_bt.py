"""
backtest/options_monthly_bt.py

Monthly iron-condor backtester that runs on ANY symbol's option cache (the NIFTY
index cache in data/.cache/nse_fo, or a single-stock cache in
data/.cache/nse_stockopt/<SYM>). Used to compare index vs single-stock premium
selling on a like-for-like MONTHLY basis (single stocks are monthly-only).

Same real-price mechanics as options_real_bt: sell a `short_pct`% OTM condor with
`wing_pct`% wings at the entry-day close, hold to expiry, settle at real intrinsic
vs the settlement underlying. Costs + slippage included. One position at a time.

    python -m backtest.options_monthly_bt --dir data/.cache/nse_stockopt/RELIANCE --lot 500
"""
from __future__ import annotations
import argparse
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

_STEP_DEFAULT = 50.0


@dataclass
class MonthlyResult:
    trades: list[dict] = field(default_factory=list)
    skipped: int = 0
    @property
    def n(self): return len(self.trades)
    @property
    def pnl(self): return [t["pnl_rs"] for t in self.trades]
    @property
    def total_rs(self): return round(sum(self.pnl), 0)
    @property
    def win_rate(self): return round(np.mean([1 if p > 0 else 0 for p in self.pnl]) * 100, 1) if self.trades else 0
    @property
    def profit_factor(self):
        g = sum(p for p in self.pnl if p > 0); l = -sum(p for p in self.pnl if p < 0)
        return round(g / l, 2) if l > 0 else float("inf")
    @property
    def worst_rs(self): return round(min(self.pnl), 0) if self.trades else 0
    @property
    def sharpe(self):
        p = np.array(self.pnl, float)
        if len(p) < 2 or p.std() == 0: return 0.0
        return round(float(p.mean() / p.std() * np.sqrt(12)), 2)  # monthly


def _step_for(spot: float) -> float:
    # NSE strike gaps scale with price; approximate reasonable rounding.
    if spot >= 20000: return 50.0
    if spot >= 5000: return 100.0
    if spot >= 2000: return 20.0
    if spot >= 500: return 10.0
    return 5.0


def _leg(day, expiry, strike, is_call):
    tp = "CE" if is_call else "PE"
    m = day[(day["XpryDt"] == expiry) & (day["OptnTp"] == tp) & (np.isclose(day["StrkPric"], strike))]
    if m.empty:
        return None
    px = float(m["ClsPric"].iloc[0])
    return px if px > 0 else None


def run_monthly_condor(cache_dir: str, lot_size: int, short_pct=5.0, wing_pct=3.0,
                       min_dte=18, max_dte=45, cost_per_leg=20.0, slip_pts_per_leg=1.0,
                       start=None, end=None, strike_step=None) -> MonthlyResult:
    d = Path(cache_dir)
    files = sorted(d.glob("*.csv"))
    dates = [datetime.strptime(f.stem, "%Y-%m-%d").date() for f in files]
    if start:
        s = datetime.strptime(start, "%Y-%m-%d").date(); files=[f for f,x in zip(files,dates) if x>=s]; dates=[x for x in dates if x>=s]
    if end:
        e = datetime.strptime(end, "%Y-%m-%d").date(); files=[f for f,x in zip(files,dates) if x<=e]; dates=[x for x in dates if x<=e]
    by = dict(zip(dates, files))
    res = MonthlyResult()
    pos = None
    for dt in dates:
        day = pd.read_csv(by[dt])
        if day.empty or "UndrlygPric" not in day.columns:
            continue
        day["XpryDt"] = pd.to_datetime(day["XpryDt"]).dt.date
        spotcol = day["UndrlygPric"].dropna()
        if spotcol.empty:
            continue
        spot = float(spotcol.iloc[0])
        step = strike_step or _step_for(spot)

        if pos is not None and dt >= pos["expiry"]:
            S = spot; p = pos
            payoff = p["credit"] - max(0, S - p["kc"]) - max(0, p["kp"] - S) \
                     + max(0, S - p["kcw"]) + max(0, p["kpw"] - S)
            cost = (cost_per_leg * 4 * 2) / lot_size + slip_pts_per_leg * 4
            res.trades.append({"entry": p["entry"], "expiry": p["expiry"],
                               "pnl_rs": round((payoff - cost) * lot_size, 0)})
            pos = None

        if pos is None:
            exps = sorted(x for x in day["XpryDt"].unique() if min_dte <= (x - dt).days <= max_dte)
            if not exps:
                continue
            expiry = exps[0]
            off = round(spot * short_pct / 100 / step) * step
            w = round(spot * wing_pct / 100 / step) * step or step
            kc = round(spot / step) * step + off
            kp = round(spot / step) * step - off
            sc, sp = _leg(day, expiry, kc, True), _leg(day, expiry, kp, False)
            bc, bp = _leg(day, expiry, kc + w, True), _leg(day, expiry, kp - w, False)
            if None in (sc, sp, bc, bp):
                res.skipped += 1; continue
            credit = sc + sp - bc - bp
            if credit <= 0:
                res.skipped += 1; continue
            pos = {"entry": dt, "expiry": expiry, "credit": credit,
                   "kc": kc, "kp": kp, "kcw": kc + w, "kpw": kp - w}
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--lot", type=int, required=True)
    ap.add_argument("--short", type=float, default=5.0)
    ap.add_argument("--wing", type=float, default=3.0)
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    args = ap.parse_args()
    r = run_monthly_condor(args.dir, args.lot, short_pct=args.short, wing_pct=args.wing,
                           start=args.start, end=args.end)
    print(f"{args.dir}: n={r.n} skip={r.skipped} win%={r.win_rate} PF={r.profit_factor} "
          f"sharpe={r.sharpe} total=Rs{r.total_rs:,.0f} worst=Rs{r.worst_rs:,.0f}")


if __name__ == "__main__":
    main()
