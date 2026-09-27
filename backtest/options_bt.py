"""
backtest/options_bt.py

Synthetic backtester for a WEEKLY defined-risk short IRON-FLY on NIFTY.

Why synthetic: Angel's instrument master is live-only (expired option tokens are
gone), so a real historical options series isn't available. Instead we price the
legs with Black-Scholes using India VIX as the implied-vol input, and settle at
expiry on the real NIFTY close. This captures the structural volatility-risk-
premium edge (IV systematically > realized) with realistic, DEFINED risk. It is
an approximation (no real skew, bid/ask, or intraday path), so treat the numbers
as directional and deliberately conservative-leaning.

Structure each cycle (one calendar week):
  - Entry: first trading day of the week. Spot S0 = that day's close, IV = VIX.
    ATM strike K = nearest 50. SELL ATM call+put, BUY wings at K±width.
    Net credit collected (points) priced via BS, T = days-to-expiry.
  - Expiry: last trading day of the week. Settle all legs at intrinsic on the
    real NIFTY close S_exp.
  - Payoff (points) = credit - |S_exp-K| + max(0,S_exp-(K+W)) + max(0,(K-W)-S_exp)
    => max profit = credit (S_exp==K); max loss = credit - W (|move|>=W). Defined.
  - Costs (points) subtracted per cycle. Margin ≈ max loss * lot_size.

Metrics: net P&L (points and ₹), win rate, worst week, max drawdown, return on
margin, and a plain short-straddle (no wings) comparison for tail context.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from strategy.options_pricing import bs_price, intrinsic, round_to_strike

_IDX_DIR = Path(__file__).parent.parent / "data" / ".cache" / "index"


@dataclass
class IronFlyConfig:
    wing_pct: float = 1.5          # wing distance as % of spot (defined-risk width)
    short_offset_pct: float = 0.0  # 0 = iron-fly (sell ATM); >0 = iron-condor (sell OTM strikes)
    lot_size: int = 75             # NIFTY lot
    iv_mult: float = 1.0           # scale VIX -> weekly IV (VIX is 30d; weekly often higher)
    r: float = 0.065
    cost_per_leg: float = 20.0     # ₹ brokerage per leg (entry+exit legs counted)
    slippage_pts_per_leg: float = 1.0  # points of slippage per leg (bid/ask)
    use_wings: bool = True         # False = naked short straddle (tail-risk comparison)
    strike_step: float = 50.0


@dataclass
class IronFlyResult:
    weeks: list[dict] = field(default_factory=list)
    lot_size: int = 75

    @property
    def n(self): return len(self.weeks)
    @property
    def pnl_pts(self): return [w["pnl_pts"] for w in self.weeks]
    @property
    def total_pts(self): return round(sum(self.pnl_pts), 1)
    @property
    def total_rs(self): return round(self.total_pts * self.lot_size, 0)
    @property
    def win_rate(self): return round(np.mean([1 if p > 0 else 0 for p in self.pnl_pts]) * 100, 1) if self.weeks else 0
    @property
    def avg_pts(self): return round(np.mean(self.pnl_pts), 2) if self.weeks else 0
    @property
    def worst_week_pts(self): return round(min(self.pnl_pts), 1) if self.weeks else 0
    @property
    def best_week_pts(self): return round(max(self.pnl_pts), 1) if self.weeks else 0
    @property
    def avg_margin_rs(self): return round(np.mean([w["margin_rs"] for w in self.weeks]), 0) if self.weeks else 0

    @property
    def max_drawdown_pts(self):
        eq = np.cumsum([0.0] + self.pnl_pts)
        peak = np.maximum.accumulate(eq)
        return round(float((eq - peak).min()), 1)

    @property
    def return_on_margin_pct(self):
        # total ₹ profit / average margin deployed, over the whole period
        if not self.weeks or self.avg_margin_rs <= 0:
            return 0.0
        return round(self.total_rs / self.avg_margin_rs * 100, 1)

    @property
    def sharpe(self):
        p = np.array(self.pnl_pts, dtype=float)
        if len(p) < 2 or p.std() == 0:
            return 0.0
        return round(float(p.mean() / p.std() * np.sqrt(52)), 2)  # weekly -> annualized


def _load_index():
    nif = pd.read_csv(_IDX_DIR / "nifty_1d.csv")
    vix = pd.read_csv(_IDX_DIR / "vix_1d.csv")
    for df in (nif, vix):
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        if df["timestamp"].dt.tz is not None:
            df["timestamp"] = df["timestamp"].dt.tz_localize(None)
        df["date"] = df["timestamp"].dt.date
    n = nif.set_index("date")["close"]
    v = vix.set_index("date")["close"]
    df = pd.DataFrame({"nifty": n, "vix": v}).dropna().sort_index()
    df.index = pd.to_datetime(df.index)
    return df


def run_iron_fly(cfg: IronFlyConfig | None = None,
                 start: str | None = None, end: str | None = None) -> IronFlyResult:
    cfg = cfg or IronFlyConfig()
    df = _load_index()
    if start:
        df = df[df.index >= pd.Timestamp(start)]
    if end:
        df = df[df.index <= pd.Timestamp(end)]

    res = IronFlyResult(lot_size=cfg.lot_size)
    # group by ISO (year, week): entry = first trading day, expiry = last
    grp = df.groupby([df.index.isocalendar().year, df.index.isocalendar().week])
    for _, wk in grp:
        if len(wk) < 2:
            continue
        entry = wk.iloc[0]
        expiry = wk.iloc[-1]
        S0 = float(entry["nifty"]); iv = float(entry["vix"]) / 100.0 * cfg.iv_mult
        S_exp = float(expiry["nifty"])
        # Time to expiry in the TRADING-DAY convention that VIX is quoted in:
        # VIX-implied 1-day move = VIX/sqrt(252), so 1 trading day = 1/252 year.
        # (Using calendar_days/365 underprices the premium and creates fake losses.)
        days_td = max(1, len(wk) - 1)   # number of forward trading-day moves held
        T = days_td / 252.0
        if S0 <= 0 or iv <= 0:
            continue
        K = round_to_strike(S0, cfg.strike_step)
        W = round_to_strike(S0 * cfg.wing_pct / 100.0, cfg.strike_step) or cfg.strike_step
        off = round_to_strike(S0 * cfg.short_offset_pct / 100.0, cfg.strike_step)
        Kc = K + off   # short call strike (>= ATM for a condor)
        Kp = K - off   # short put strike

        # entry credit (points): sell call@Kc + put@Kp, buy wings beyond
        sell_c = bs_price(S0, Kc, T, iv, True, cfg.r)
        sell_p = bs_price(S0, Kp, T, iv, False, cfg.r)
        credit = sell_c + sell_p
        n_legs = 2
        if cfg.use_wings:
            buy_c = bs_price(S0, Kc + W, T, iv, True, cfg.r)
            buy_p = bs_price(S0, Kp - W, T, iv, False, cfg.r)
            credit -= (buy_c + buy_p)
            n_legs = 4

        # expiry payoff (points)
        payoff = credit - intrinsic(S_exp, Kc, True) - intrinsic(S_exp, Kp, False)
        if cfg.use_wings:
            payoff += intrinsic(S_exp, Kc + W, True) + intrinsic(S_exp, Kp - W, False)

        # costs in points: brokerage (₹ -> pts) + slippage
        cost_rs = cfg.cost_per_leg * n_legs * 2   # entry + exit legs
        cost_pts = cost_rs / cfg.lot_size + cfg.slippage_pts_per_leg * n_legs
        pnl_pts = payoff - cost_pts

        # defined-risk margin ≈ max loss
        max_loss_pts = (W - credit) if cfg.use_wings else (0.15 * S0)  # naked ~ SPAN approx
        margin_rs = max(max_loss_pts, 1) * cfg.lot_size

        res.weeks.append({
            "entry_date": entry.name.date(), "expiry_date": expiry.name.date(),
            "S0": round(S0, 1), "S_exp": round(S_exp, 1), "K": K, "W": W,
            "vix": round(float(entry["vix"]), 1), "credit_pts": round(credit, 1),
            "move_pts": round(abs(S_exp - S0), 1), "pnl_pts": round(pnl_pts, 2),
            "margin_rs": round(margin_rs, 0),
        })
    return res
