"""
backtest/options_multi_bt.py

Regime-aware, multi-strategy options backtester on REAL NSE bhavcopy prices.

Motivation: a single premium-selling structure (iron condor) is only profitable in
calm regimes and bleeds/blows up when volatility returns (proved in
backtest/options_real_bt.py). The idea here is to hold a LIBRARY of option
structures and route each weekly cycle to the structure that fits the regime known
AT ENTRY (no lookahead):
  - calm + range-bound  -> SELL premium (iron condor / iron fly)
  - trending up         -> bull put credit spread  (sell downside)
  - trending down       -> bear call credit spread (sell upside)
  - high / rising vol   -> BUY premium (long strangle) or stay FLAT

Every structure is a list of Legs (strike, is_call, qty: +1 long / -1 short).
Entry is priced at each leg's real ClsPric on the entry day; expiry is settled at
real intrinsic vs the settlement underlying. Costs + slippage included. All
structures are DEFINED-RISK (bounded loss) so they fit a small account.

Regime signals use only data available on/at the entry day:
  - VIX level + 60d VIX percentile (calm vs stressed)
  - VIX 5d change (rising vol => avoid selling)
  - NIFTY trend: close vs 20d SMA, and 10d momentum
  - realized 10d vol vs VIX (VRP richness)

Usage:
    python -m backtest.options_multi_bt              # standalone + mix comparison
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

_FO_DIR = Path(__file__).parent.parent / "data" / ".cache" / "nse_fo"
_IDX_DIR = Path(__file__).parent.parent / "data" / ".cache" / "index"
_STEP = 50.0
_LOT = 75
_COST_PER_LEG = 20.0      # ₹ brokerage per leg
_SLIP_PTS_PER_LEG = 1.0   # points slippage per leg


# ----------------------------- structures ---------------------------------
@dataclass(frozen=True)
class Leg:
    strike: float
    is_call: bool
    qty: int  # +1 long (pay), -1 short (collect)


def _r(x: float) -> float:
    return round(x / _STEP) * _STEP


def iron_condor(spot, short_off_pct, wing_pct):
    off = _r(spot * short_off_pct / 100) or _STEP
    W = _r(spot * wing_pct / 100) or _STEP
    kc, kp = _r(spot) + off, _r(spot) - off
    return [Leg(kc, True, -1), Leg(kc + W, True, +1),
            Leg(kp, False, -1), Leg(kp - W, False, +1)]


def iron_fly(spot, wing_pct):
    W = _r(spot * wing_pct / 100) or _STEP
    k = _r(spot)
    return [Leg(k, True, -1), Leg(k + W, True, +1),
            Leg(k, False, -1), Leg(k - W, False, +1)]


def bull_put_spread(spot, short_off_pct, wing_pct):
    off = _r(spot * short_off_pct / 100) or _STEP
    W = _r(spot * wing_pct / 100) or _STEP
    kp = _r(spot) - off
    return [Leg(kp, False, -1), Leg(kp - W, False, +1)]


def bear_call_spread(spot, short_off_pct, wing_pct):
    off = _r(spot * short_off_pct / 100) or _STEP
    W = _r(spot * wing_pct / 100) or _STEP
    kc = _r(spot) + off
    return [Leg(kc, True, -1), Leg(kc + W, True, +1)]


def long_strangle(spot, off_pct, wing_pct=None):
    off = _r(spot * off_pct / 100) or _STEP
    kc, kp = _r(spot) + off, _r(spot) - off
    return [Leg(kc, True, +1), Leg(kp, False, +1)]


def long_iron_strangle(spot, off_pct, wing_pct):
    """Long strangle financed by selling further-OTM wings => DEFINED cost & payoff.
    Reverse iron condor: buy near-OTM call+put, sell far-OTM call+put."""
    off = _r(spot * off_pct / 100) or _STEP
    W = _r(spot * wing_pct / 100) or _STEP
    kc, kp = _r(spot) + off, _r(spot) - off
    return [Leg(kc, True, +1), Leg(kc + W, True, -1),
            Leg(kp, False, +1), Leg(kp - W, False, -1)]


# ----------------------------- pricing/settle ------------------------------
def _leg_close(day: pd.DataFrame, expiry, strike, is_call) -> float | None:
    tp = "CE" if is_call else "PE"
    m = day[(day["XpryDt"] == expiry) & (day["OptnTp"] == tp)
            & (np.isclose(day["StrkPric"], strike))]
    if m.empty:
        return None
    px = float(m["ClsPric"].iloc[0])
    return px if px >= 0 else None


def _entry_cost(day, expiry, legs: list[Leg]):
    """Net debit(+)/credit(-) in points to OPEN. Returns (net_debit, ok)."""
    net = 0.0
    for lg in legs:
        px = _leg_close(day, expiry, lg.strike, lg.is_call)
        if px is None or (lg.qty < 0 and px <= 0):
            return None, False  # can't sell a zero-bid leg / missing strike
        net += lg.qty * px      # long pays (+), short collects (-)
    return net, True


def _expiry_value(S: float, legs: list[Leg]) -> float:
    """Intrinsic value of the position at expiry (points), signed by qty."""
    v = 0.0
    for lg in legs:
        intr = max(0.0, S - lg.strike) if lg.is_call else max(0.0, lg.strike - S)
        v += lg.qty * intr
    return v


def _defined_risk(legs, entry_debit) -> float:
    """Max loss (points) of a defined-risk structure, brute-forced over strikes."""
    strikes = sorted({lg.strike for lg in legs})
    grid = [0.0] + strikes + [s for s in strikes] + [max(strikes) * 2]
    worst = 0.0
    for S in grid:
        pnl = _expiry_value(S, legs) - entry_debit  # value received - cost paid
        worst = min(worst, pnl)
    return abs(worst)


# ----------------------------- data / regime -------------------------------
def _load_index():
    nif = pd.read_csv(_IDX_DIR / "nifty_1d.csv")
    vix = pd.read_csv(_IDX_DIR / "vix_1d.csv")
    for df in (nif, vix):
        df["date"] = pd.to_datetime(df["timestamp"]).dt.tz_localize(None).dt.date
    n = nif.set_index("date")["close"].sort_index()
    v = vix.set_index("date")["close"].sort_index()
    df = pd.DataFrame({"nifty": n, "vix": v}).dropna()
    df["sma20"] = df["nifty"].rolling(20).mean()
    df["mom10"] = df["nifty"].pct_change(10)
    df["ret"] = df["nifty"].pct_change()
    df["rv10"] = df["ret"].rolling(10).std() * np.sqrt(252) * 100  # annualized %
    df["vix_pct60"] = df["vix"].rolling(60).apply(
        lambda w: (w.rank(pct=True).iloc[-1]) * 100, raw=False)
    df["vix_chg5"] = df["vix"] - df["vix"].shift(5)
    return df


@dataclass
class Regime:
    label: str
    vix: float
    vix_pct: float
    trend: str  # up/down/flat


def classify(idx_row) -> Regime:
    vix = float(idx_row["vix"])
    vpct = float(idx_row["vix_pct60"]) if not np.isnan(idx_row["vix_pct60"]) else 50.0
    close, sma, mom = idx_row["nifty"], idx_row["sma20"], idx_row["mom10"]
    trend = "flat"
    if not np.isnan(sma):
        if close > sma and mom > 0.01:
            trend = "up"
        elif close < sma and mom < -0.01:
            trend = "down"
    rising = (not np.isnan(idx_row["vix_chg5"])) and idx_row["vix_chg5"] > 2.0
    if vpct >= 80 or rising:
        label = "volatile"
    elif trend == "up":
        label = "trend_up"
    elif trend == "down":
        label = "trend_down"
    else:
        label = "calm"
    return Regime(label, vix, vpct, trend)


# ----------------------------- backtester ----------------------------------
@dataclass
class MultiResult:
    weeks: list[dict] = field(default_factory=list)
    skipped: int = 0

    @property
    def n(self): return len(self.weeks)
    @property
    def pnl(self): return [w["pnl_rs"] for w in self.weeks]
    @property
    def total_rs(self): return round(sum(self.pnl), 0)
    @property
    def win_rate(self):
        traded = [w for w in self.weeks if w["strat"] != "flat"]
        if not traded: return 0.0
        return round(np.mean([1 if w["pnl_rs"] > 0 else 0 for w in traded]) * 100, 1)
    @property
    def profit_factor(self):
        g = sum(p for p in self.pnl if p > 0); l = -sum(p for p in self.pnl if p < 0)
        return round(g / l, 2) if l > 0 else float("inf")
    @property
    def worst_rs(self): return round(min(self.pnl), 0) if self.weeks else 0
    @property
    def best_rs(self): return round(max(self.pnl), 0) if self.weeks else 0
    @property
    def avg_margin(self):
        m = [w["margin_rs"] for w in self.weeks if w["strat"] != "flat"]
        return round(float(np.mean(m)), 0) if m else 0
    @property
    def max_dd(self):
        eq = np.cumsum([0.0] + self.pnl); peak = np.maximum.accumulate(eq)
        return round(float((eq - peak).min()), 0)
    @property
    def sharpe(self):
        p = np.array([w["pnl_rs"] for w in self.weeks if w["strat"] != "flat"], float)
        if len(p) < 2 or p.std() == 0: return 0.0
        return round(float(p.mean() / p.std() * np.sqrt(52)), 2)
    @property
    def n_traded(self): return len([w for w in self.weeks if w["strat"] != "flat"])


StrategyRouter = Callable[[float, "Regime"], "list[Leg] | None"]


def _load_day(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["XpryDt"] = pd.to_datetime(df["XpryDt"]).dt.date
    return df


def backtest(router: StrategyRouter, start=None, end=None,
             min_dte=3, max_dte=10) -> MultiResult:
    files = sorted(_FO_DIR.glob("*.csv"))
    dates = [datetime.strptime(f.stem, "%Y-%m-%d").date() for f in files]
    by_date = dict(zip(dates, files))
    idx = _load_index()
    if start:
        s = datetime.strptime(start, "%Y-%m-%d").date(); dates = [d for d in dates if d >= s]
    if end:
        e = datetime.strptime(end, "%Y-%m-%d").date(); dates = [d for d in dates if d <= e]

    res = MultiResult()
    open_pos = None
    for d in dates:
        day = _load_day(by_date[d])
        spot_col = day["UndrlygPric"].dropna()
        if spot_col.empty:
            continue
        spot = float(spot_col.iloc[0])

        # settle
        if open_pos is not None and d >= open_pos["expiry"]:
            p = open_pos
            if p["strat"] == "_flat_placeholder":
                open_pos = None
            else:
                val = _expiry_value(spot, p["legs"])
                pnl_pts = val - p["debit"] - p["cost_pts"]
                res.weeks.append({
                    "entry": p["entry"], "expiry": p["expiry"], "strat": p["strat"],
                    "regime": p["regime"], "S0": round(p["S0"], 0), "S_exp": round(spot, 0),
                    "pnl_rs": round(pnl_pts * _LOT, 0), "margin_rs": round(p["margin"], 0),
                })
                open_pos = None

        # open
        if open_pos is None:
            if d not in idx.index:
                prior = [x for x in idx.index if x <= d]
                if not prior:
                    continue
                irow = idx.loc[prior[-1]]
            else:
                irow = idx.loc[d]
            reg = classify(irow)
            exps = sorted(x for x in day["XpryDt"].unique() if min_dte <= (x - d).days <= max_dte)
            if not exps:
                continue
            expiry = exps[0]

            choice = router(spot, reg)  # None => flat, else (name, legs)
            if choice is None:
                res.weeks.append({"entry": d, "expiry": expiry, "strat": "flat",
                                  "regime": reg.label, "S0": round(spot, 0),
                                  "S_exp": round(spot, 0), "pnl_rs": 0.0, "margin_rs": 0.0})
                open_pos = {"entry": d, "expiry": expiry, "legs": [], "debit": 0.0,
                            "cost_pts": 0.0, "margin": 0.0, "strat": "_flat_placeholder",
                            "regime": reg.label, "S0": spot}
                continue
            name, legs = choice
            debit, ok = _entry_cost(day, expiry, legs)
            if not ok:
                res.skipped += 1
                continue
            n_legs = len(legs)
            cost_pts = (_COST_PER_LEG * n_legs * 2) / _LOT + _SLIP_PTS_PER_LEG * n_legs
            margin = _defined_risk(legs, debit) * _LOT
            open_pos = {"entry": d, "expiry": expiry, "legs": legs, "debit": debit,
                        "cost_pts": cost_pts, "margin": margin, "strat": name,
                        "regime": reg.label, "S0": spot}
    return res
