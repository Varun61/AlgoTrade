"""
options/vix_filter.py

The VIX-timing gate that turns the plain iron condor into the durable edge:
only SELL premium when it is rich, i.e. when India VIX sits in the upper part of
its recent range. Selling every week (no gate) tested at ~4.5%/yr with a −75k
drawdown; gating to the top ~45% of the VIX range tested at ~15-17%/yr — the gate
IS the edge, so it must be enforced live exactly as in the backtest.

Pure, testable helpers (no I/O). The live VIX series is supplied by the caller
(the daily runner fetches it); here we only do the math.
"""
from __future__ import annotations

from options.registry import VIX_PCTL_WINDOW, WEEKLY_VIX_MIN_PCTL


def vix_percentile(vix_series: list[float], window: int = VIX_PCTL_WINDOW) -> float | None:
    """
    Percentile rank (0-100) of the LATEST VIX value within the trailing `window`
    values (inclusive), matching the backtest's rolling 60-day percentile.
    Returns None if there isn't enough history.
    """
    if not vix_series or len(vix_series) < 2:
        return None
    w = vix_series[-window:] if len(vix_series) >= window else vix_series[:]
    latest = w[-1]
    # fraction of the window <= latest, as a percentage (rank-percentile)
    rank = sum(1 for v in w if v <= latest) / len(w)
    return round(rank * 100.0, 1)


def should_sell(vix_series: list[float], threshold: float = WEEKLY_VIX_MIN_PCTL,
                window: int = VIX_PCTL_WINDOW) -> tuple[bool, float | None]:
    """
    Decide whether to open the weekly condor today.
    Returns (sell?, vix_pctl). If history is insufficient, returns (False, None) —
    we conservatively SIT OUT rather than trade blind.
    """
    pctl = vix_percentile(vix_series, window)
    if pctl is None:
        return False, None
    return pctl >= threshold, pctl
