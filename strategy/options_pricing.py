"""
strategy/options_pricing.py

Black-Scholes European option pricing — pure, dependency-light functions used by
the synthetic options backtester (backtest/options_bt.py). Indian index options
(NIFTY) are European-style and cash-settled, so BS is appropriate.

Everything here is a pure function (no I/O, no API) so it's fast and unit-testable.
"""

from __future__ import annotations
import math


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via erf (no scipy dependency)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(spot: float, strike: float, t_years: float, sigma: float,
             is_call: bool, r: float = 0.065) -> float:
    """
    Black-Scholes price of a European option.

    Args:
        spot     : underlying price
        strike   : strike price
        t_years  : time to expiry in YEARS (e.g. 3/365 for 3 calendar days)
        sigma    : annualized implied volatility as a fraction (e.g. 0.15 for 15%)
        is_call  : True for call, False for put
        r        : risk-free rate (annual), default ~6.5% (India)

    Returns:
        Option premium (>= intrinsic). At/near expiry (t->0) returns intrinsic.
    """
    if t_years <= 0 or sigma <= 0:
        return intrinsic(spot, strike, is_call)
    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t_years) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    disc = math.exp(-r * t_years)
    if is_call:
        return spot * _norm_cdf(d1) - strike * disc * _norm_cdf(d2)
    return strike * disc * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def intrinsic(spot: float, strike: float, is_call: bool) -> float:
    """Intrinsic value at expiry (also the cash-settlement value)."""
    return max(0.0, spot - strike) if is_call else max(0.0, strike - spot)


def straddle_price(spot: float, strike: float, t_years: float, sigma: float, r: float = 0.065) -> float:
    """ATM (or any-strike) straddle premium = call + put at the same strike."""
    return bs_price(spot, strike, t_years, sigma, True, r) + bs_price(spot, strike, t_years, sigma, False, r)


def round_to_strike(spot: float, step: float = 50.0) -> float:
    """Nearest tradeable strike (NIFTY strikes are spaced 50 pts)."""
    return round(spot / step) * step
