import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from execution.position_manager import PositionManager, find_weakest_position


def _pos(token, direction, entry_price, stop_loss):
    return {"token": token, "symbol": token, "direction": direction, "qty": 1,
            "entry_price": entry_price, "stop_loss": stop_loss}


def test_find_weakest_position_picks_long_nearest_its_stop():
    positions = [
        _pos("A", "long", entry_price=100.0, stop_loss=90.0),   # 10 pts risk, ltp=95 -> 50% left
        _pos("B", "long", entry_price=100.0, stop_loss=90.0),   # ltp=91 -> 10% left (weakest)
    ]
    current_ltps = {"A": 95.0, "B": 91.0}
    assert find_weakest_position(positions, current_ltps) == "B"


def test_find_weakest_position_picks_short_nearest_its_stop():
    positions = [
        _pos("A", "short", entry_price=100.0, stop_loss=110.0),  # ltp=105 -> 50% left
        _pos("B", "short", entry_price=100.0, stop_loss=110.0),  # ltp=109 -> 10% left (weakest)
    ]
    current_ltps = {"A": 105.0, "B": 109.0}
    assert find_weakest_position(positions, current_ltps) == "B"


def test_find_weakest_position_uses_live_stop_over_static_one():
    # Static stop says A is fine, but its live (trailed) stop is nearly hit.
    positions = [_pos("A", "long", entry_price=100.0, stop_loss=90.0)]
    current_ltps = {"A": 101.0}
    live_stops = {"A": 100.5}  # trailed stop now above ltp — effectively already breached
    assert find_weakest_position(positions, current_ltps, live_stops) == "A"


def test_find_weakest_position_returns_none_when_no_positions():
    assert find_weakest_position([], {}) is None


def test_find_weakest_position_skips_zero_risk_positions():
    # entry == stop_loss (zero risk denominator) must not crash and must be skipped
    positions = [
        _pos("A", "long", entry_price=100.0, stop_loss=100.0),
        _pos("B", "long", entry_price=100.0, stop_loss=95.0),
    ]
    current_ltps = {"A": 105.0, "B": 96.0}
    assert find_weakest_position(positions, current_ltps) == "B"


def test_find_weakest_position_integrates_with_position_manager():
    pm = PositionManager()
    pm.open_position(symbol="A-EQ", token="A", direction="long", qty=1,
                      entry_price=100.0, stop_loss=90.0, target=120.0, order_id="1")
    pm.open_position(symbol="B-EQ", token="B", direction="long", qty=1,
                      entry_price=100.0, stop_loss=90.0, target=120.0, order_id="2")
    current_ltps = {"A": 98.0, "B": 91.0}
    assert find_weakest_position(pm.get_all_positions(), current_ltps) == "B"
