"""
options/registry.py

THE SINGLE SOURCE OF TRUTH for every tradable options strategy.

Each strategy is one or more `Component`s. A Component is one repeatable trade
unit with its own real-data P&L stream (per single lot) and its own margin. A
strategy that blends two edges (the HYBRID) simply lists two Components; the
sizer then decides how many lots of each to run for a given capital.

To ADD A NEW STRATEGY: write a builder that returns an OptionsStrategy and add it
to REGISTRY at the bottom. It is then selectable by name in config/settings.yaml
(`options.strategy: <name>`) with no other code changes.

All P&L comes from real NSE option prices via the backtesters in backtest/.
NIFTY lot = 75 (absolute ₹ scale only; %/PF/Sharpe are lot-independent).

----------------------------------------------------------------------------
STRATEGY MENU (minimum capital = enough for 1 lot with a survivable tail)
----------------------------------------------------------------------------
  weekly  — VIX-timed iron condor, held to weekly expiry. Sells a 3%-OTM condor
            with 2%-wide wings ONLY when India VIX is in the upper ~60% of its
            60-day range (premium is rich). Execution-tolerant.
            MIN CAPITAL ≈ ₹1,00,000   | margin ≈ ₹37,000/lot
  daily   — 0DTE iron-fly. On expiry day, sell the ATM straddle + 1%-wide wings
            at the open and let it decay to the close. Capital-light but needs
            tight (~2pt/leg) fills. VIX-timed the same way.
            MIN CAPITAL ≈ ₹60,000     | margin ≈ ₹12,000/lot
  hybrid  — weekly + daily together. They are only ~0.18 correlated, so blending
            earns more per unit of drawdown. Needs enough capital to run at least
            one of each.
            MIN CAPITAL ≈ ₹2,00,000   | margin ≈ ₹49,000 (1 weekly + 1 daily)
----------------------------------------------------------------------------
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import pandas as pd

from backtest.options_multi_bt import backtest as _weekly_bt, iron_condor
from backtest.options_intraday_bt import run_intraday as _intraday_bt

LOT_SIZE = 75

# ---- Weekly condor parameters — THE SINGLE SOURCE OF TRUTH ----
# The durable, validated edge (5yr real data): sell a 3%-OTM iron condor with 2%
# wings, held to weekly expiry, ONLY when India VIX is in the upper part of its
# 60-day range (rich premium). Both the backtest and the live/paper runner import
# these so they can never drift apart.
WEEKLY_SHORT_PCT = 3.0
WEEKLY_WING_PCT = 2.0
WEEKLY_VIX_MIN_PCTL = 55.0     # only sell when VIX 60-day percentile >= this
VIX_PCTL_WINDOW = 60


@dataclass
class Component:
    """One repeatable single-lot trade unit with its real-data P&L stream."""
    label: str                 # 'weekly' | 'daily'
    per_lot_pnl: pd.Series     # indexed by settlement date, ₹ per single lot
    margin_per_lot: float      # ₹ blocked per lot (approx max loss / SPAN)


@dataclass
class OptionsStrategy:
    name: str
    kind: str                  # 'weekly' | 'daily' | 'hybrid'
    description: str
    min_capital: int
    components: list[Component] = field(default_factory=list)
    warning: str = ""          # non-empty => shown loudly by the runner

    @property
    def margin_one_of_each(self) -> float:
        return sum(c.margin_per_lot for c in self.components)


# ---------------------------------------------------------------------------
# Component builders (run the real-data backtest, return a per-lot ₹ P&L series)
# ---------------------------------------------------------------------------
def _weekly_component(start=None, end=None, vix_min=WEEKLY_VIX_MIN_PCTL,
                      short_pct=WEEKLY_SHORT_PCT, wing_pct=WEEKLY_WING_PCT) -> Component:
    r = _weekly_bt(
        lambda spot, reg: (("condor", iron_condor(spot, short_pct, wing_pct))
                           if reg.vix_pct >= vix_min else None),
        start=start, end=end,
    )
    ser = pd.Series({pd.Timestamp(x["expiry"]): x["pnl_rs"]
                     for x in r.weeks if x["strat"] != "flat"}).sort_index()
    margin = _p90([x["margin_rs"] for x in r.weeks if x["strat"] != "flat"], 37000)
    return Component("weekly", ser, margin)


def _daily_component(start=None, end=None, vix_min=40.0,
                     wing_pct=1.0, slip=2.0) -> Component:
    r = _intraday_bt("straddle", use_wings=True, wing_pct=wing_pct,
                     vix_min=vix_min, slip_pts_per_leg=slip, start=start, end=end)
    ser = pd.Series({pd.Timestamp(x["date"]): x["pnl_rs"] for x in r.days}).sort_index()
    margin = _p90([x["margin_rs"] for x in r.days], 12000)
    return Component("daily", ser, margin)


def _p90(vals, fallback):
    if not vals:
        return float(fallback)
    import numpy as np
    return float(np.percentile(vals, 90))


# ---------------------------------------------------------------------------
# Strategy builders
# ---------------------------------------------------------------------------
# min_capital = worst single-trade loss / 0.25 (the default 25% drawdown budget),
# i.e. enough that one worst historical trade is <=25% of the account.
#
# 5-YEAR VERDICT (real NSE data 2021-07..2026-09, incl. the 2022 selloff):
#   weekly (vix>=55) is the ONLY strategy positive across the window — PF 1.87,
#   Sharpe 1.23, ~₹18k/yr per lot, positive in 5 of 6 years (only 2023 lost).
#   daily 0DTE and the daily+weekly hybrid LOSE over 5 years; they only worked in
#   the calm 2024-2026 regime. They are kept for research but carry a warning and
#   must NOT be used with real money on the current evidence.
def build_weekly(start=None, end=None) -> OptionsStrategy:
    return OptionsStrategy(
        name="weekly", kind="weekly", min_capital=110_000,  # worst week ~₹27k / 0.25
        description="VIX-timed (>=55th pct) 3%/2% iron condor, held to weekly expiry. "
                    "~₹18k/yr per lot over 5yr; the durable, recommended strategy.",
        components=[_weekly_component(start, end)],
    )


def build_daily(start=None, end=None) -> OptionsStrategy:
    return OptionsStrategy(
        name="daily", kind="daily", min_capital=80_000,  # worst day ~₹19k / 0.25
        description="0DTE ATM iron-fly (1% wings), entered at open on expiry day, VIX-timed.",
        warning="FAILED the 5-year test — loses 2021-2023, only profitable in the "
                "2024-2026 calm regime. Not for real money on current evidence.",
        components=[_daily_component(start, end)],
    )


def build_hybrid(start=None, end=None) -> OptionsStrategy:
    return OptionsStrategy(
        name="hybrid", kind="hybrid", min_capital=160_000,  # combined worst DD ~₹38k / 0.25
        description="Weekly condor + daily 0DTE together (~0.18 correlated).",
        warning="The daily leg fails the 5-year test, dragging the hybrid below the "
                "weekly-only result over 2021-2026. Prefer 'weekly' for real money.",
        components=[_weekly_component(start, end), _daily_component(start, end)],
    )


# name -> builder(start, end) -> OptionsStrategy
REGISTRY: dict[str, Callable[..., OptionsStrategy]] = {
    "weekly": build_weekly,
    "daily": build_daily,
    "hybrid": build_hybrid,
}


def list_strategies() -> list[str]:
    return list(REGISTRY)


def get_strategy(name: str, start=None, end=None) -> OptionsStrategy:
    key = (name or "").strip().lower()
    if key not in REGISTRY:
        raise ValueError(f"Unknown options strategy '{name}'. "
                         f"Available: {', '.join(REGISTRY)}")
    return REGISTRY[key](start=start, end=end)
