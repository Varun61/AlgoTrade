import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from risk.position_sizer import PositionSizer
from risk.circuit_breaker import CircuitBreaker, CircuitStatus


# ----------------------------------------------------------------------
# PositionSizer
# ----------------------------------------------------------------------

def test_compute_qty_matches_hand_calculation():
    # max_position_value set high so the cap doesn't interfere with the raw formula
    sizer = PositionSizer(capital=100_000, per_trade_risk_pct=1.0, max_position_value=1_000_000)
    # risk_amount = 100000*0.01 = 1000; stop_distance = 5; qty = floor(1000/5) = 200
    qty = sizer.compute_qty(entry_price=450.0, stop_loss=445.0)
    assert qty == 200


def test_compute_qty_zero_on_near_zero_stop_distance():
    sizer = PositionSizer(capital=100_000, per_trade_risk_pct=1.0)
    assert sizer.compute_qty(entry_price=450.0, stop_loss=450.005) == 0


def test_compute_qty_respects_lot_size_flooring():
    sizer = PositionSizer(capital=100_000, per_trade_risk_pct=1.0, lot_size=75,
                           max_position_value=1_000_000)
    # risk_amount=1000, stop_distance=5 -> raw_qty=200 -> floor(200/75)*75=150
    qty = sizer.compute_qty(entry_price=450.0, stop_loss=445.0)
    assert qty == 150


def test_compute_qty_capped_by_max_position_value():
    sizer = PositionSizer(capital=100_000, per_trade_risk_pct=5.0, max_position_value=10_000)
    # risk_amount=5000, stop_distance=1 -> raw_qty=5000 -> position value 5000*450 >> cap
    qty = sizer.compute_qty(entry_price=450.0, stop_loss=449.0)
    assert qty * 450.0 <= 10_000 + 1e-6
    assert qty == int(10_000 // 450.0)


def test_compute_qty_default_max_position_value_is_half_capital():
    sizer = PositionSizer(capital=100_000, per_trade_risk_pct=100.0)  # force huge raw_qty
    qty = sizer.compute_qty(entry_price=10.0, stop_loss=9.0)
    assert qty * 10.0 <= 50_000 + 1e-6


def test_update_capital_changes_subsequent_sizing():
    sizer = PositionSizer(capital=100_000, per_trade_risk_pct=1.0, max_position_value=1_000_000)
    sizer.update_capital(50_000)
    qty = sizer.compute_qty(entry_price=450.0, stop_loss=445.0)
    assert qty == 100  # risk_amount halves to 500 -> 500/5=100


# ----------------------------------------------------------------------
# CircuitBreaker
# ----------------------------------------------------------------------

def test_can_trade_ok_initially():
    cb = CircuitBreaker(capital=100_000)
    ok, reason = cb.can_trade()
    assert ok is True
    assert reason == ""


def test_daily_loss_limit_halts_trading():
    cb = CircuitBreaker(capital=100_000, daily_loss_limit_pct=2.0)
    cb.on_trade_close(pnl=-2100.0)  # -2.1% > -2.0% limit
    ok, reason = cb.can_trade()
    assert ok is False
    assert cb.status == CircuitStatus.HALTED
    assert "Daily loss limit" in reason


def test_daily_loss_limit_not_yet_halted_below_threshold():
    cb = CircuitBreaker(capital=100_000, daily_loss_limit_pct=2.0)
    cb.on_trade_close(pnl=-1000.0)  # -1.0% < 2.0% limit
    ok, _ = cb.can_trade()
    assert ok is True


def test_max_trades_per_day_blocks():
    cb = CircuitBreaker(capital=100_000, max_trades_per_day=2)
    cb.on_trade_close(pnl=100.0)
    cb.on_trade_close(pnl=100.0)
    ok, reason = cb.can_trade()
    assert ok is False
    assert "Max trades/day" in reason


def test_max_concurrent_positions_blocks():
    cb = CircuitBreaker(capital=100_000, max_concurrent=2)
    cb.on_trade_open()
    cb.on_trade_open()
    ok, reason = cb.can_trade()
    assert ok is False
    assert "Max concurrent" in reason


def test_consecutive_losses_halts_and_resets_on_win():
    cb = CircuitBreaker(capital=100_000, max_consecutive_losses=3)
    cb.on_trade_close(pnl=-10)
    cb.on_trade_close(pnl=-10)
    assert cb.consecutive_losses == 2
    ok, _ = cb.can_trade()
    assert ok is True  # not yet halted (limit is 3)

    cb.on_trade_close(pnl=100)  # a win resets the streak
    assert cb.consecutive_losses == 0

    cb.on_trade_close(pnl=-10)
    cb.on_trade_close(pnl=-10)
    cb.on_trade_close(pnl=-10)
    ok, reason = cb.can_trade()
    assert ok is False
    assert "Consecutive losses" in reason
    assert cb.status == CircuitStatus.HALTED


def test_manual_resume_only_clears_consecutive_loss_halt():
    cb = CircuitBreaker(capital=100_000, max_consecutive_losses=2)
    cb.on_trade_close(pnl=-10)
    cb.on_trade_close(pnl=-10)
    cb.can_trade()  # triggers halt evaluation
    assert cb.status == CircuitStatus.HALTED

    cb.manual_resume()
    assert cb.status == CircuitStatus.OK
    assert cb.consecutive_losses == 0


def test_manual_resume_does_not_clear_daily_loss_halt():
    cb = CircuitBreaker(capital=100_000, daily_loss_limit_pct=1.0)
    cb.on_trade_close(pnl=-2000.0)
    cb.can_trade()
    assert cb.status == CircuitStatus.HALTED

    cb.manual_resume()
    assert cb.status == CircuitStatus.HALTED  # daily loss halt requires reset_session


def test_reset_session_clears_all_state():
    cb = CircuitBreaker(capital=100_000, daily_loss_limit_pct=1.0)
    cb.on_trade_open()
    cb.on_trade_close(pnl=-2000.0)
    cb.can_trade()
    assert cb.status == CircuitStatus.HALTED

    cb.reset_session(new_capital=90_000)
    assert cb.status == CircuitStatus.OK
    assert cb.realized_pnl == 0.0
    assert cb.trades_today == 0
    assert cb.open_positions == 0
    assert cb.capital == 90_000


def test_daily_pnl_pct_property():
    cb = CircuitBreaker(capital=100_000)
    cb.on_trade_close(pnl=-500.0)
    assert cb.daily_pnl_pct == pytest.approx(-0.5)


def test_open_positions_never_goes_negative():
    cb = CircuitBreaker(capital=100_000)
    cb.on_trade_close(pnl=100.0)  # close without a matching open
    assert cb.open_positions == 0
