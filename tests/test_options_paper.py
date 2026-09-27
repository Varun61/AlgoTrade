"""Unit tests for execution/options_paper.py (paper iron-condor engine)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from execution.options_paper import (
    build_condor_strikes, entry_credit, expiry_pnl_points, condor_costs_points,
    CondorLegs, PaperCondorBook, round_to_step,
)


def test_build_condor_strikes_ordering():
    legs = build_condor_strikes(spot=23140, offset_pct=2.0, wing_pct=1.0)
    # short strikes OTM either side of ATM(23150); longs further out
    assert legs.long_pe_strike < legs.short_pe_strike < legs.short_ce_strike < legs.long_ce_strike
    assert legs.short_ce_strike > 23150 and legs.short_pe_strike < 23150
    # wings are `wing_pct` beyond the shorts
    assert legs.long_ce_strike - legs.short_ce_strike == pytest.approx(round_to_step(23140*0.01))
    assert legs.short_pe_strike - legs.long_pe_strike == pytest.approx(round_to_step(23140*0.01))


def test_entry_credit():
    prem = {"short_ce": 50, "short_pe": 55, "long_ce": 20, "long_pe": 22}
    assert entry_credit(prem) == pytest.approx((50 + 55) - (20 + 22))


def test_expiry_pnl_max_profit_when_between_shorts():
    legs = build_condor_strikes(23150, 2.0, 1.0)
    credit = 40.0
    # spot lands at center -> both shorts & longs expire worthless -> keep full credit
    assert expiry_pnl_points(legs, credit, 23150) == pytest.approx(credit)


def test_expiry_pnl_bounded_max_loss_on_big_move():
    legs = build_condor_strikes(23150, 2.0, 1.0)
    credit = 40.0
    wing_width = legs.long_ce_strike - legs.short_ce_strike
    # huge up move: loss is capped at credit - wing_width (defined risk)
    pnl_up = expiry_pnl_points(legs, credit, 40000)
    assert pnl_up == pytest.approx(credit - wing_width)
    pnl_dn = expiry_pnl_points(legs, credit, 5000)
    assert pnl_dn == pytest.approx(credit - wing_width)
    # loss never worse than -(wing_width - credit)
    assert pnl_up >= -(wing_width) and pnl_dn >= -(wing_width)


def test_costs_positive():
    assert condor_costs_points(lot_size=65) > 0


def test_paper_book_open_settle_summary(tmp_path):
    book = PaperCondorBook(path=tmp_path / "paper.jsonl", lot_size=65)
    legs = build_condor_strikes(23150, 2.0, 1.0)
    prem = {"short_ce": 60, "short_pe": 60, "long_ce": 25, "long_pe": 25}  # credit = 70
    credit = book.open_condor("2026-09-29", legs, prem, 23150)
    assert credit == pytest.approx(70.0)
    # settle at center -> profit ~ credit - costs
    book.settle_condor("2026-09-29", legs, credit, 23150)
    s = book.summary()
    assert s["trades"] == 1
    assert s["total_pnl_rs"] > 0            # profitable when spot stays between shorts
    assert s["win_rate"] == 100.0


def test_paper_book_big_loss_is_capped(tmp_path):
    book = PaperCondorBook(path=tmp_path / "p.jsonl", lot_size=65)
    legs = build_condor_strikes(23150, 2.0, 1.0)
    prem = {"short_ce": 60, "short_pe": 60, "long_ce": 25, "long_pe": 25}
    credit = book.open_condor("2026-09-29", legs, prem, 23150)
    book.settle_condor("2026-09-29", legs, credit, 40000)  # crash-up scenario
    s = book.summary()
    wing = legs.long_ce_strike - legs.short_ce_strike
    # loss per lot bounded by wing width (defined risk), in rupees
    assert s["total_pnl_rs"] >= -(wing * 65) - 500
