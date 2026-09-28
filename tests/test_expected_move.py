"""
tests/test_expected_move.py

Guards the expected-move variant + the paper/backtest comparison plumbing:
- expected_move / em_condor produce sensible, vol-adaptive strikes
- build_em_condor_strikes yields a valid 4-leg condor
- weekly_em is registered and sizes
- compute_iv_rv math
- the paper-trade pairing (OPEN+SETTLE) used by the comparison tool
"""
import json

from backtest.options_multi_bt import expected_move, em_condor
from execution.options_paper import build_em_condor_strikes
from options.registry import list_strategies, get_strategy
from tools.run_options_paper import compute_iv_rv


def test_expected_move_scales_with_vix():
    # higher VIX => larger expected move
    assert expected_move(24000, 20) > expected_move(24000, 10)


def test_em_condor_is_four_legs_and_ordered():
    legs = em_condor(24000, 15, short_mult=1.0, wing_mult=1.0)
    assert len(legs) == 4
    calls = [l for l in legs if l.is_call]; puts = [l for l in legs if not l.is_call]
    # each side: one short (qty<0) nearer, one long (qty>0) further out
    sc = [l for l in calls if l.qty < 0][0]; lc = [l for l in calls if l.qty > 0][0]
    sp = [l for l in puts if l.qty < 0][0]; lp = [l for l in puts if l.qty > 0][0]
    assert lc.strike > sc.strike > 24000 > sp.strike > lp.strike


def test_build_em_condor_strikes_geometry():
    legs = build_em_condor_strikes(24000, 15, 1.0, 1.0)
    assert legs.long_ce_strike > legs.short_ce_strike
    assert legs.long_pe_strike < legs.short_pe_strike
    assert legs.short_ce_strike > legs.short_pe_strike


def test_weekly_em_registered_and_sizes():
    assert "weekly_em" in list_strategies()
    s = get_strategy("weekly_em", start="2024-08-01")
    assert len(s.components) == 1
    assert s.warning  # experimental -> must carry a warning


def test_compute_iv_rv():
    # flat prices -> zero realized vol -> ratio defaults to 1.0
    assert compute_iv_rv(15.0, [100.0] * 20) == 1.0
    # some movement -> positive realized vol -> finite ratio
    closes = [100, 101, 99, 102, 98, 103, 97, 104, 96, 105, 95]
    r = compute_iv_rv(15.0, closes)
    assert r > 0


def test_paper_trade_pairing(tmp_path):
    from tools.compare_paper_backtest import _paper_trades
    log = tmp_path / "p.jsonl"
    with open(log, "w") as f:
        f.write(json.dumps({"event": "OPEN", "expiry": "2026-10-07", "credit_pts": 30.0}) + "\n")
        f.write(json.dumps({"event": "SETTLE", "expiry": "2026-10-07", "pnl_rs": 1500}) + "\n")
        f.write(json.dumps({"event": "OPEN", "expiry": "2026-10-14", "credit_pts": 25.0}) + "\n")
    trades = _paper_trades(log)
    assert len(trades) == 1  # only the settled one is a completed trade
    assert trades[0]["pnl_rs"] == 1500 and trades[0]["credit_pts"] == 30.0
