"""
backtest/options_calendar_bt.py

REAL-DATA backtester for the CALENDAR (time) spread on NIFTY:
  SELL the near-week option, BUY the far-month option at the SAME strike.

This is a structurally different engine from the condor: the two legs have
DIFFERENT expiries, so at the near expiry the short leg settles to intrinsic while
the long (monthly) leg is STILL ALIVE and must be valued at its real market price
that day. It profits when price pins near the strike at near-expiry (short decays
to ~0 while the long keeps its time value) and/or when volatility rises (long vega).

Mechanics (per cycle, one position at a time):
  entry day T:
    K       = ATM strike from UndrlygPric
    near    = nearest weekly expiry, dte in [near_min, near_max]
    far     = monthly-ish expiry,   dte in [far_min, far_max]
    short_entry = real ClsPric(K, near, type) ; long_entry = real ClsPric(K, far, type)
    debit   = long_entry - short_entry   (calendars are a net debit)
  exit day = near expiry date:
    short_exit = intrinsic(S, K)                 (near settles at expiry)
    long_exit  = real ClsPric(K, far, type)      (monthly still trades)
    pnl_pts    = (short_entry - short_exit) + (long_exit - long_entry) - costs
  Max loss ~= net debit (defined). Margin ~= debit.

Variants: 'call', 'put', or 'double' (both a call and a put calendar = long vol,
range-profit around K).

    python -m backtest.options_calendar_bt --type double --start 2021-07-16
"""
from __future__ import annotations
import argparse
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

_FO_DIR = Path(__file__).parent.parent / "data" / ".cache" / "nse_fo"
_STEP = 50.0
_LOT = 75
_COST_PER_LEG = 20.0
_SLIP_PTS_PER_LEG = 1.0


def _rnd(x): return round(x / _STEP) * _STEP


@dataclass
class CalResult:
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
    def avg_margin(self):
        m = [t["margin_rs"] for t in self.trades]
        return round(float(np.mean(m)), 0) if m else 0
    @property
    def sharpe(self):
        p = np.array(self.pnl, float)
        if len(p) < 2 or p.std() == 0: return 0.0
        return round(float(p.mean() / p.std() * np.sqrt(52)), 2)


def _close(day, expiry, strike, is_call):
    tp = "CE" if is_call else "PE"
    m = day[(day["XpryDt"] == expiry) & (day["OptnTp"] == tp) & (np.isclose(day["StrkPric"], strike))]
    if m.empty:
        return None
    px = float(m["ClsPric"].iloc[0])
    return px if px > 0 else None


def _load(path):
    df = pd.read_csv(path)
    df["XpryDt"] = pd.to_datetime(df["XpryDt"]).dt.date
    return df


def _one_leg_pnl(day_entry, day_exit, K, near, far, is_call, S_exp):
    se = _close(day_entry, near, K, is_call)   # short near entry
    le = _close(day_entry, far, K, is_call)    # long far entry
    if se is None or le is None:
        return None
    # sanity filter: monthly ATM should be ~1.2-3x the weekly ATM. A far price far
    # outside that is a stale/erroneous bhavcopy print for the illiquid strike.
    if le > 3.0 * se or le < se:
        return None
    lx = _close(day_exit, far, K, is_call)     # long far exit (still trading)
    if lx is None:
        return None
    intrinsic = max(0.0, S_exp - K) if is_call else max(0.0, K - S_exp)
    sx = intrinsic                              # short settles at intrinsic
    lx = max(lx, intrinsic)                     # long can't be worth less than intrinsic
    debit = le - se
    pnl = (se - sx) + (lx - le)
    return pnl, debit


def run_calendar(kind="double", near_min=3, near_max=9, far_min=22, far_max=40,
                 start=None, end=None) -> CalResult:
    files = sorted(_FO_DIR.glob("*.csv"))
    dates = [datetime.strptime(f.stem, "%Y-%m-%d").date() for f in files]
    by = dict(zip(dates, files))
    if start:
        s = datetime.strptime(start, "%Y-%m-%d").date(); dates = [d for d in dates if d >= s]
    if end:
        e = datetime.strptime(end, "%Y-%m-%d").date(); dates = [d for d in dates if d <= e]

    res = CalResult()
    pos = None
    for d in dates:
        day = _load(by[d])
        sc = day["UndrlygPric"].dropna()
        if sc.empty:
            continue
        spot = float(sc.iloc[0])

        if pos is not None and d >= pos["near"]:
            legs = [True, False] if kind == "double" else [kind == "call"]
            total = 0.0; debit_sum = 0.0; ok = True
            for is_call in legs:
                r = _one_leg_pnl(pos["day_entry"], day, pos["K"], pos["near"], pos["far"], is_call, spot)
                if r is None:
                    ok = False; break
                total += r[0]; debit_sum += r[1]
            if ok:
                nlegs = 4 if kind == "double" else 2
                cost = (_COST_PER_LEG * nlegs * 2) / _LOT + _SLIP_PTS_PER_LEG * nlegs
                res.trades.append({"entry": pos["entry"], "near": pos["near"],
                                   "pnl_rs": round((total - cost) * _LOT, 0),
                                   "margin_rs": round(max(debit_sum, 1) * _LOT, 0)})
            else:
                res.skipped += 1
            pos = None

        if pos is None:
            exps = sorted(day["XpryDt"].unique())
            near = next((x for x in exps if near_min <= (x - d).days <= near_max), None)
            far = next((x for x in exps if far_min <= (x - d).days <= far_max), None)
            if near is None or far is None or near >= far:
                continue
            pos = {"entry": d, "day_entry": day, "near": near, "far": far, "K": _rnd(spot)}
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--type", default="double", choices=["call", "put", "double"])
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    args = ap.parse_args()
    r = run_calendar(args.type, start=args.start, end=args.end)
    print(f"calendar({args.type}): n={r.n} skip={r.skipped} win%={r.win_rate} PF={r.profit_factor} "
          f"sharpe={r.sharpe} total=Rs{r.total_rs:,.0f} worst=Rs{r.worst_rs:,.0f} margin=Rs{r.avg_margin:,.0f}")


if __name__ == "__main__":
    main()
