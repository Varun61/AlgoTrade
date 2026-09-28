"""
backtest/options_intraday_bt.py

SAME-DAY (intraday / 0DTE) options backtester on real NSE bhavcopy OHLC.

Idea the user asked to test: buy AND sell options on the SAME day — enter at the
day's OPEN, exit at the day's CLOSE, no overnight risk. The highest-theta version
is 0DTE: on an expiry day, sell the options that expire THAT day and let them
decay into the close.

Data: each contract's real OpnPric / HghPric / LwPric / ClsPric for the day.
Strike selection uses the NIFTY INDEX open (data/.cache/index) so there is no
lookahead (the bhavcopy's UndrlygPric is an EOD value; the index open is the real
morning level).

P&L (points), short leg = collect at open, buy back at close:  Opn - Cls
        long leg  = pay at open, sell at close:                Cls - Opn
Optional intraday STOP approximated from the leg's HghPric/LwPric: if a short
leg's high implies the position lost >= stop x credit at some point, we assume we
were stopped near that level.

Structures (all entered/exited same day):
  - short straddle  : sell ATM call+put
  - short strangle  : sell ~off% OTM call+put
  - iron fly/condor : short straddle/strangle + long protective wings (defined risk)

Usage:
    python -m backtest.options_intraday_bt
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

_FO_DIR = Path(__file__).parent.parent / "data" / ".cache" / "nse_fo"
_IDX_DIR = Path(__file__).parent.parent / "data" / ".cache" / "index"
_STEP = 50.0
_LOT = 75
_COST_PER_LEG = 20.0
_SLIP_PTS_PER_LEG = 1.0


def _r(x: float) -> float:
    return round(x / _STEP) * _STEP


def _load_index():
    nif = pd.read_csv(_IDX_DIR / "nifty_1d.csv")
    vix = pd.read_csv(_IDX_DIR / "vix_1d.csv")
    for df in (nif, vix):
        df["date"] = pd.to_datetime(df["timestamp"]).dt.tz_localize(None).dt.date
    n = nif.set_index("date")
    v = vix.set_index("date")["close"]
    out = pd.DataFrame({"open": n["open"], "close": n["close"], "vix": v}).dropna()
    out["vix_pct60"] = out["vix"].rolling(60).apply(lambda w: w.rank(pct=True).iloc[-1] * 100)
    return out


def _load_day(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["XpryDt"] = pd.to_datetime(df["XpryDt"]).dt.date
    return df


def _leg(day, expiry, strike, is_call):
    tp = "CE" if is_call else "PE"
    m = day[(day["XpryDt"] == expiry) & (day["OptnTp"] == tp)
            & (np.isclose(day["StrkPric"], strike))]
    if m.empty:
        return None
    row = m.iloc[0]
    o, h, l, c = float(row["OpnPric"]), float(row["HghPric"]), float(row["LwPric"]), float(row["ClsPric"])
    if o <= 0:
        return None
    return {"o": o, "h": h, "l": l, "c": c}


@dataclass
class IntradayResult:
    days: list[dict] = field(default_factory=list)

    @property
    def n(self): return len(self.days)
    @property
    def pnl(self): return [x["pnl_rs"] for x in self.days]
    @property
    def total_rs(self): return round(sum(self.pnl), 0)
    @property
    def win_rate(self):
        return round(np.mean([1 if p > 0 else 0 for p in self.pnl]) * 100, 1) if self.days else 0
    @property
    def profit_factor(self):
        g = sum(p for p in self.pnl if p > 0); l = -sum(p for p in self.pnl if p < 0)
        return round(g / l, 2) if l > 0 else float("inf")
    @property
    def worst_rs(self): return round(min(self.pnl), 0) if self.days else 0
    @property
    def best_rs(self): return round(max(self.pnl), 0) if self.days else 0
    @property
    def avg_rs(self): return round(float(np.mean(self.pnl)), 0) if self.days else 0
    @property
    def max_dd(self):
        eq = np.cumsum([0.0] + self.pnl); peak = np.maximum.accumulate(eq)
        return round(float((eq - peak).min()), 0)
    @property
    def sharpe(self):
        p = np.array(self.pnl, float)
        if len(p) < 2 or p.std() == 0: return 0.0
        return round(float(p.mean() / p.std() * np.sqrt(252)), 2)  # daily -> annualized


def run_intraday(structure="strangle", off_pct=0.5, wing_pct=1.0, use_wings=False,
                 expiry_only=True, vix_min=None, vix_max=None,
                 stop_mult=None, start=None, end=None,
                 slip_pts_per_leg=_SLIP_PTS_PER_LEG) -> IntradayResult:
    """
    structure: 'straddle' (ATM) or 'strangle' (off_pct OTM)
    use_wings: add long protective wings wing_pct beyond shorts (defined risk)
    expiry_only: only trade on 0DTE expiry days (highest theta)
    vix_min/max: only trade when VIX 60d-percentile in [min,max]
    stop_mult: intraday stop at stop_mult x credit (approx via leg highs)
    """
    files = sorted(_FO_DIR.glob("*.csv"))
    dates = [datetime.strptime(f.stem, "%Y-%m-%d").date() for f in files]
    by_date = dict(zip(dates, files))
    idx = _load_index()
    if start:
        s = datetime.strptime(start, "%Y-%m-%d").date(); dates = [d for d in dates if d >= s]
    if end:
        e = datetime.strptime(end, "%Y-%m-%d").date(); dates = [d for d in dates if d <= e]

    res = IntradayResult()
    for d in dates:
        if d not in idx.index:
            continue
        irow = idx.loc[d]
        vpct = float(irow["vix_pct60"]) if not np.isnan(irow["vix_pct60"]) else 50.0
        if vix_min is not None and vpct < vix_min:
            continue
        if vix_max is not None and vpct > vix_max:
            continue
        open_spot = float(irow["open"])
        day = _load_day(by_date[d])

        # pick expiry: same-day (0DTE) if expiry_only else nearest available
        exps = sorted(day["XpryDt"].unique())
        if expiry_only:
            if d not in exps:
                continue
            expiry = d
        else:
            fwd = [x for x in exps if x >= d]
            if not fwd:
                continue
            expiry = fwd[0]

        off = _r(open_spot * off_pct / 100) if structure == "strangle" else 0.0
        W = _r(open_spot * wing_pct / 100) or _STEP
        kc = _r(open_spot) + off
        kp = _r(open_spot) - off

        sc = _leg(day, expiry, kc, True)
        sp = _leg(day, expiry, kp, False)
        if sc is None or sp is None:
            continue
        # short legs: collect open, pay close
        credit = sc["o"] + sp["o"]
        pnl = (sc["o"] - sc["c"]) + (sp["o"] - sp["c"])
        n_legs = 2
        max_adverse = (sc["h"] - sc["o"]) + (sp["h"] - sp["o"])  # rough worst intraday
        if use_wings:
            lc = _leg(day, expiry, kc + W, True)
            lp = _leg(day, expiry, kp - W, False)
            if lc is None or lp is None:
                continue
            credit -= (lc["o"] + lp["o"])
            pnl += (lc["c"] - lc["o"]) + (lp["c"] - lp["o"])
            n_legs = 4
        if credit <= 0:
            continue

        # optional intraday stop: if position lost >= stop_mult*credit at the highs
        if stop_mult is not None and max_adverse >= stop_mult * credit:
            pnl = -stop_mult * credit  # assume stopped at the stop level

        cost_pts = (_COST_PER_LEG * n_legs * 2) / _LOT + slip_pts_per_leg * n_legs
        pnl -= cost_pts
        margin = (W - credit) * _LOT if use_wings else 0.10 * open_spot * _LOT
        res.days.append({
            "date": d, "expiry": expiry, "open_spot": round(open_spot, 0),
            "credit": round(credit, 1), "pnl_rs": round(pnl * _LOT, 0),
            "margin_rs": round(max(margin, 1), 0),
        })
    return res


def _row(tag, r: IntradayResult):
    print(f"{tag:<30} n={r.n:<4} win%={r.win_rate:<5} PF={r.profit_factor:<5} sh={r.sharpe:<6} "
          f"Rs{r.total_rs:>9,.0f} avg={r.avg_rs:>6,.0f} worst={r.worst_rs:>8,.0f} DD={r.max_dd:>9,.0f}")


def main():
    print("=== SAME-DAY 0DTE (enter at open, exit at close, expiry days only) ===")
    _row("short straddle ATM", run_intraday("straddle"))
    _row("short strangle 0.5%", run_intraday("strangle", off_pct=0.5))
    _row("short strangle 1.0%", run_intraday("strangle", off_pct=1.0))
    _row("iron-fly ATM w1%", run_intraday("straddle", use_wings=True, wing_pct=1.0))
    _row("iron-condor 0.5/1%", run_intraday("strangle", off_pct=0.5, use_wings=True, wing_pct=1.0))
    _row("iron-condor 1/1%", run_intraday("strangle", off_pct=1.0, use_wings=True, wing_pct=1.0))

    print("\n=== 0DTE iron-fly ATM w1% + intraday stop ===")
    for sm in [1.0, 1.5, 2.0]:
        _row(f"stop {sm}x credit", run_intraday("straddle", use_wings=True, wing_pct=1.0, stop_mult=sm))

    print("\n=== 0DTE iron-fly ATM w1% by year ===")
    for tag, s, e in [("2024H2", None, "2024-12-31"), ("2025H1", "2025-01-01", "2025-06-30"),
                      ("2025H2", "2025-07-01", "2025-12-31"), ("2026H1", "2026-01-01", "2026-06-30"),
                      ("2026H2", "2026-07-01", None)]:
        _row(tag, run_intraday("straddle", use_wings=True, wing_pct=1.0, start=s, end=e))


if __name__ == "__main__":
    main()
