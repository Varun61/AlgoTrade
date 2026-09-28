"""
backtest/options_real_bt.py

REAL-DATA backtester for the weekly defined-risk short IRON CONDOR on NIFTY.

Unlike backtest/options_bt.py (which prices legs synthetically with Black-Scholes
+ VIX because Angel's master is live-only), this reads ACTUAL settled option
prices from NSE's F&O bhavcopy archive, cached per day in data/.cache/nse_fo/ by
tools/fetch_nse_fo.py. Every entry credit is a real traded close price, and every
expiry payoff is the real intrinsic value against the real settlement underlying.
This is the honest test of whether the volatility-risk-premium edge survives real
skew, real strike granularity, and real tail weeks.

Cycle logic (no overlapping positions):
  - Walk trading days in order. When flat, OPEN a condor on day T targeting the
    nearest WEEKLY expiry E (T+3..T+10 days out).
      * spot = UndrlygPric on day T
      * SHORT strikes ~short_offset_pct OTM (call above, put below), rounded to 50
      * LONG wings wing_pct further out
      * entry credit (points) = (short_call + short_put) - (long_call + long_put),
        each leg = that contract's real ClsPric on day T
  - At day >= E, SETTLE at real intrinsic vs UndrlygPric (== settlement) on E:
      pnl_pts = credit
                - max(0, S-short_call) - max(0, short_put-S)
                + max(0, S-long_call)  + max(0, long_put-S)
  - Costs (points) subtracted per cycle. Margin ≈ (wing_width - credit) * lot.

Usage:
    python -m backtest.options_real_bt --short-offset 2.0 --wing 1.0
"""
from __future__ import annotations
import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

_FO_DIR = Path(__file__).parent.parent / "data" / ".cache" / "nse_fo"


@dataclass
class RealCondorConfig:
    short_offset_pct: float = 2.0   # short strikes this % OTM (0 => iron-fly ATM)
    wing_pct: float = 1.0           # long wings this % beyond the shorts
    lot_size: int = 75              # NIFTY lot (75 from Apr-2025; scale only, not edge)
    strike_step: float = 50.0
    min_dte: int = 3                # nearest weekly must be >= this many days out
    max_dte: int = 10               # ... and <= this (skip monthly-only weeks)
    cost_per_leg: float = 20.0      # ₹ brokerage per leg
    slippage_pts_per_leg: float = 1.0
    use_wings: bool = True          # False => naked short strangle (tail comparison)


@dataclass
class RealCondorResult:
    weeks: list[dict] = field(default_factory=list)
    lot_size: int = 75
    skipped: int = 0

    @property
    def n(self): return len(self.weeks)
    @property
    def pnl_pts(self): return [w["pnl_pts"] for w in self.weeks]
    @property
    def total_pts(self): return round(sum(self.pnl_pts), 1)
    @property
    def total_rs(self): return round(self.total_pts * self.lot_size, 0)
    @property
    def win_rate(self):
        return round(np.mean([1 if p > 0 else 0 for p in self.pnl_pts]) * 100, 1) if self.weeks else 0
    @property
    def avg_pts(self): return round(float(np.mean(self.pnl_pts)), 2) if self.weeks else 0
    @property
    def worst_week_pts(self): return round(min(self.pnl_pts), 1) if self.weeks else 0
    @property
    def best_week_pts(self): return round(max(self.pnl_pts), 1) if self.weeks else 0
    @property
    def avg_margin_rs(self):
        return round(float(np.mean([w["margin_rs"] for w in self.weeks])), 0) if self.weeks else 0
    @property
    def profit_factor(self):
        gains = sum(p for p in self.pnl_pts if p > 0)
        losses = -sum(p for p in self.pnl_pts if p < 0)
        return round(gains / losses, 2) if losses > 0 else float("inf")

    @property
    def max_drawdown_pts(self):
        eq = np.cumsum([0.0] + self.pnl_pts)
        peak = np.maximum.accumulate(eq)
        return round(float((eq - peak).min()), 1)

    @property
    def return_on_margin_pct(self):
        if not self.weeks or self.avg_margin_rs <= 0:
            return 0.0
        return round(self.total_rs / self.avg_margin_rs * 100, 1)

    @property
    def sharpe(self):
        p = np.array(self.pnl_pts, dtype=float)
        if len(p) < 2 or p.std() == 0:
            return 0.0
        return round(float(p.mean() / p.std() * np.sqrt(52)), 2)


def _round(x: float, step: float) -> float:
    return round(x / step) * step


def _load_day(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["XpryDt"] = pd.to_datetime(df["XpryDt"]).dt.date
    df["TradDt"] = pd.to_datetime(df["TradDt"]).dt.date
    return df


def _leg_price(day: pd.DataFrame, expiry, strike: float, is_call: bool) -> float | None:
    """Real close price of one contract on a given day; None if not found/illiquid."""
    tp = "CE" if is_call else "PE"
    m = day[(day["XpryDt"] == expiry) & (day["OptnTp"] == tp)
            & (np.isclose(day["StrkPric"], strike))]
    if m.empty:
        return None
    px = float(m["ClsPric"].iloc[0])
    return px if px > 0 else None


def run_real_condor(cfg: RealCondorConfig | None = None,
                    start: str | None = None, end: str | None = None) -> RealCondorResult:
    cfg = cfg or RealCondorConfig()
    files = sorted(_FO_DIR.glob("*.csv"))
    dates = [datetime.strptime(f.stem, "%Y-%m-%d").date() for f in files]
    if start:
        s = datetime.strptime(start, "%Y-%m-%d").date()
        files = [f for f, d in zip(files, dates) if d >= s]
        dates = [d for d in dates if d >= s]
    if end:
        e = datetime.strptime(end, "%Y-%m-%d").date()
        files = [f for f, d in zip(files, dates) if d <= e]
        dates = [d for d in dates if d <= e]

    by_date = dict(zip(dates, files))
    res = RealCondorResult(lot_size=cfg.lot_size)

    open_pos = None
    for i, d in enumerate(dates):
        day = _load_day(by_date[d])
        spot_col = day["UndrlygPric"].dropna()
        if spot_col.empty:
            continue
        spot = float(spot_col.iloc[0])

        # --- settle an open position that has reached expiry ---
        if open_pos is not None and d >= open_pos["expiry"]:
            S = spot
            p = open_pos
            payoff = p["credit"]
            payoff -= max(0.0, S - p["Kc"]) + max(0.0, p["Kp"] - S)
            if cfg.use_wings:
                payoff += max(0.0, S - p["Kc_w"]) + max(0.0, p["Kp_w"] - S)
            n_legs = 4 if cfg.use_wings else 2
            cost_pts = (cfg.cost_per_leg * n_legs * 2) / cfg.lot_size \
                       + cfg.slippage_pts_per_leg * n_legs
            pnl_pts = payoff - cost_pts
            max_loss_pts = (p["W"] - p["credit"]) if cfg.use_wings else 0.15 * p["S0"]
            res.weeks.append({
                "entry_date": p["entry"], "expiry_date": p["expiry"],
                "S0": round(p["S0"], 1), "S_exp": round(S, 1),
                "Kc": p["Kc"], "Kp": p["Kp"], "W": p["W"],
                "credit_pts": round(p["credit"], 1),
                "move_pts": round(S - p["S0"], 1),
                "pnl_pts": round(pnl_pts, 2),
                "margin_rs": round(max(max_loss_pts, 1) * cfg.lot_size, 0),
            })
            open_pos = None

        # --- open a new position when flat ---
        if open_pos is None:
            expiries = sorted(x for x in day["XpryDt"].unique()
                              if cfg.min_dte <= (x - d).days <= cfg.max_dte)
            if not expiries:
                continue
            expiry = expiries[0]
            off = _round(spot * cfg.short_offset_pct / 100.0, cfg.strike_step)
            W = _round(spot * cfg.wing_pct / 100.0, cfg.strike_step) or cfg.strike_step
            Kc = _round(spot, cfg.strike_step) + off
            Kp = _round(spot, cfg.strike_step) - off
            sc = _leg_price(day, expiry, Kc, True)
            sp = _leg_price(day, expiry, Kp, False)
            if sc is None or sp is None:
                res.skipped += 1
                continue
            credit = sc + sp
            Kc_w = Kc + W
            Kp_w = Kp - W
            if cfg.use_wings:
                bc = _leg_price(day, expiry, Kc_w, True)
                bp = _leg_price(day, expiry, Kp_w, False)
                if bc is None or bp is None:
                    res.skipped += 1
                    continue
                credit -= (bc + bp)
            if credit <= 0:
                res.skipped += 1
                continue
            open_pos = {"entry": d, "expiry": expiry, "S0": spot,
                        "Kc": Kc, "Kp": Kp, "Kc_w": Kc_w, "Kp_w": Kp_w,
                        "W": W, "credit": credit}
    return res


def _fmt(res: RealCondorResult, tag: str) -> str:
    return (f"{tag}\n"
            f"  cycles={res.n}  skipped={res.skipped}\n"
            f"  win%={res.win_rate}  PF={res.profit_factor}  sharpe={res.sharpe}\n"
            f"  total={res.total_pts}pts = Rs{res.total_rs:,.0f}  avg/wk={res.avg_pts}pts\n"
            f"  best={res.best_week_pts}  worst={res.worst_week_pts}  maxDD={res.max_drawdown_pts}pts\n"
            f"  avg margin=Rs{res.avg_margin_rs:,.0f}  return on margin={res.return_on_margin_pct}%")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--short-offset", type=float, default=2.0)
    ap.add_argument("--wing", type=float, default=1.0)
    ap.add_argument("--lot", type=int, default=75)
    ap.add_argument("--naked", action="store_true", help="naked strangle (no wings)")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    args = ap.parse_args()
    cfg = RealCondorConfig(short_offset_pct=args.short_offset, wing_pct=args.wing,
                           lot_size=args.lot, use_wings=not args.naked)
    res = run_real_condor(cfg, start=args.start, end=args.end)
    label = f"REAL NIFTY {'strangle' if args.naked else 'iron condor'} " \
            f"(short {args.short_offset}% OTM, wing {args.wing}%)"
    print(_fmt(res, label))


if __name__ == "__main__":
    main()
