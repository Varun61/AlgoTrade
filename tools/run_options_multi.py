"""
tools/run_options_multi.py

Compare standalone option structures vs a regime-routed MIX on real NSE data.
Each "router" maps (spot, regime) -> (name, legs) or None (stay flat).

    python -m tools.run_options_multi
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtest.options_multi_bt import (
    backtest, iron_condor, iron_fly, bull_put_spread, bear_call_spread,
    long_strangle, long_iron_strangle, MultiResult,
)


# ---- standalone routers (always trade the same structure) ----
def always(name, builder):
    def router(spot, reg):
        return name, builder(spot)
    router.__name__ = name
    return router


R_CONDOR = always("condor3/2", lambda s: iron_condor(s, 3.0, 2.0))
R_CONDOR_TIGHT = always("condor2/1", lambda s: iron_condor(s, 2.0, 1.0))
R_FLY = always("ironfly/2", lambda s: iron_fly(s, 2.0))
R_BULLPUT = always("bullput3/2", lambda s: bull_put_spread(s, 3.0, 2.0))
R_BEARCALL = always("bearcall3/2", lambda s: bear_call_spread(s, 3.0, 2.0))
R_LONGISTR = always("longstrangle2/2", lambda s: long_iron_strangle(s, 2.0, 2.0))


# ---- regime-routed MIX ----
def mix_router(spot, reg):
    """
    calm       -> iron condor (sell premium, defined risk)
    trend_up   -> bull put credit spread (sell downside)
    trend_down -> bear call credit spread (sell upside)
    volatile   -> long iron-strangle (BUY premium, defined cost) — profit on big move
    """
    if reg.label == "calm":
        return "condor3/2", iron_condor(spot, 3.0, 2.0)
    if reg.label == "trend_up":
        return "bullput3/2", bull_put_spread(spot, 3.0, 2.0)
    if reg.label == "trend_down":
        return "bearcall3/2", bear_call_spread(spot, 3.0, 2.0)
    if reg.label == "volatile":
        return "longstrangle2/2", long_iron_strangle(spot, 2.0, 2.0)
    return None
mix_router.__name__ = "MIX(regime)"


def mix_sell_or_flat(spot, reg):
    """Conservative: sell premium only in calm/trend regimes; FLAT when volatile."""
    if reg.label == "calm":
        return "condor3/2", iron_condor(spot, 3.0, 2.0)
    if reg.label == "trend_up":
        return "bullput3/2", bull_put_spread(spot, 3.0, 2.0)
    if reg.label == "trend_down":
        return "bearcall3/2", bear_call_spread(spot, 3.0, 2.0)
    return None  # volatile -> stay flat
mix_sell_or_flat.__name__ = "MIX(sell/flat)"


def vix_timed_condor(spot, reg):
    """
    THE candidate that survives every sub-period: sell the 3%/2% iron condor ONLY
    when implied vol is rich (VIX in the upper ~60% of its 60-day range). This is
    the textbook volatility-risk-premium timing — collect fat premium when the
    market is fearful, stay flat when premium is thin. Positive PF in every 6-month
    window (unlike the always-on condor). CAVEAT: one full-width breach still loses
    ~₹27k, so it needs ~₹1.5-2L capital to size safely — NOT ₹25k.
    """
    if reg.vix_pct >= 40:
        return "condor3/2", iron_condor(spot, 3.0, 2.0)
    return None
vix_timed_condor.__name__ = "VIX-timed condor"


def _row(tag, r: MultiResult):
    print(f"{tag:<20} n={r.n_traded:<3}/{r.n:<3} win%={r.win_rate:<5} PF={r.profit_factor:<5} "
          f"sh={r.sharpe:<6} Rs{r.total_rs:>9,.0f} worst={r.worst_rs:>8,.0f} "
          f"DD={r.max_dd:>9,.0f} margin=Rs{r.avg_margin:,.0f}")


def _windows(router):
    return [
        ("full", None, None),
        ("2024H2", None, "2024-12-31"),
        ("2025H1", "2025-01-01", "2025-06-30"),
        ("2025H2", "2025-07-01", "2025-12-31"),
        ("2026H1", "2026-01-01", "2026-06-30"),
        ("2026H2", "2026-07-01", None),
    ]


def main():
    print("=== STANDALONE structures (real NSE data, full period) ===")
    for tag, R in [("condor 3/2", R_CONDOR), ("condor 2/1", R_CONDOR_TIGHT),
                   ("iron-fly /2", R_FLY), ("bull-put 3/2", R_BULLPUT),
                   ("bear-call 3/2", R_BEARCALL), ("long-strangle", R_LONGISTR)]:
        _row(tag, backtest(R))

    print("\n=== REGIME-ROUTED MIXES (full period) ===")
    _row("MIX all-regime", backtest(mix_router))
    _row("MIX sell/flat", backtest(mix_sell_or_flat))
    _row("VIX-timed condor", backtest(vix_timed_condor))

    print("\n=== VIX-timed condor sub-period stability (the survivor) ===")
    for tag, s, e in _windows(vix_timed_condor):
        _row(tag, backtest(vix_timed_condor, start=s, end=e))

    print("\n=== MIX(sell/flat) sub-period stability ===")
    for tag, s, e in _windows(mix_sell_or_flat):
        _row(tag, backtest(mix_sell_or_flat, start=s, end=e))

    print("\n=== MIX(all-regime) sub-period stability ===")
    for tag, s, e in _windows(mix_router):
        _row(tag, backtest(mix_router, start=s, end=e))


if __name__ == "__main__":
    main()
