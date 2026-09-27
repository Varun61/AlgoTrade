"""Unit tests for strategy/options_pricing.py (Black-Scholes pricer)."""
import sys, math
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from strategy.options_pricing import bs_price, intrinsic, straddle_price, round_to_strike


def test_put_call_parity():
    # C - P = S - K*e^{-rT}
    S, K, T, sig, r = 20000, 20000, 30/365, 0.15, 0.065
    c = bs_price(S, K, T, sig, True, r)
    p = bs_price(S, K, T, sig, False, r)
    assert (c - p) == pytest.approx(S - K * math.exp(-r * T), abs=1e-6)


def test_atm_call_positive_and_reasonable():
    c = bs_price(20000, 20000, 7/365, 0.15, True)
    # ~ 0.4 * S * sigma * sqrt(T) rough ATM approx
    approx = 0.4 * 20000 * 0.15 * math.sqrt(7/365)
    assert c > 0
    assert c == pytest.approx(approx, rel=0.4)


def test_expiry_returns_intrinsic():
    assert bs_price(20100, 20000, 0.0, 0.15, True) == pytest.approx(100.0)
    assert bs_price(19900, 20000, 0.0, 0.15, False) == pytest.approx(100.0)
    assert bs_price(19900, 20000, 0.0, 0.15, True) == pytest.approx(0.0)


def test_zero_vol_returns_intrinsic():
    assert bs_price(20100, 20000, 30/365, 0.0, True) == pytest.approx(100.0)


def test_intrinsic():
    assert intrinsic(105, 100, True) == 5
    assert intrinsic(95, 100, True) == 0
    assert intrinsic(95, 100, False) == 5
    assert intrinsic(105, 100, False) == 0


def test_higher_vol_raises_premium():
    lo = bs_price(20000, 20000, 30/365, 0.10, True)
    hi = bs_price(20000, 20000, 30/365, 0.25, True)
    assert hi > lo


def test_straddle_is_call_plus_put():
    S, K, T, sig = 20000, 20050, 7/365, 0.15
    assert straddle_price(S, K, T, sig) == pytest.approx(
        bs_price(S, K, T, sig, True) + bs_price(S, K, T, sig, False))


def test_round_to_strike():
    assert round_to_strike(20033) == 20050
    assert round_to_strike(20024) == 20000
    assert round_to_strike(20077, 100) == 20100
