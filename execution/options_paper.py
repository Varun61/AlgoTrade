"""
execution/options_paper.py

Paper-mode iron-condor engine for NIFTY weekly options. NO live orders — it
selects the condor legs, records a paper entry at REAL premiums, and settles at
expiry against the real NIFTY close, logging everything to logs/options_paper.jsonl
so we can measure the real (not synthetic) edge before risking money.

Pure, testable core (leg selection + P&L) + a small persistence book. The live
glue (fetching premiums/spot) lives in tools/run_options_paper.py.
"""
from __future__ import annotations
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_STEP = 50.0
_LOG = Path(__file__).parent.parent / "logs" / "options_paper.jsonl"


def round_to_step(x: float, step: float = _STEP) -> float:
    return round(x / step) * step


@dataclass
class CondorLegs:
    """The 4 legs of a short iron condor (sell OTM strangle, buy protective wings)."""
    short_ce_strike: float
    short_pe_strike: float
    long_ce_strike: float
    long_pe_strike: float
    # optional broker identifiers, filled when built from an instrument master
    symbols: dict = field(default_factory=dict)   # role -> tradingsymbol
    tokens: dict = field(default_factory=dict)     # role -> token


def build_condor_strikes(spot: float, offset_pct: float, wing_pct: float,
                         step: float = _STEP) -> CondorLegs:
    """
    Iron condor around `spot`:
      short strikes offset_pct% OTM either side; long wings a further wing_pct% out.
    """
    atm = round_to_step(spot, step)
    off = max(step, round_to_step(spot * offset_pct / 100.0, step))
    wing = max(step, round_to_step(spot * wing_pct / 100.0, step))
    short_ce = atm + off
    short_pe = atm - off
    return CondorLegs(
        short_ce_strike=short_ce, short_pe_strike=short_pe,
        long_ce_strike=short_ce + wing, long_pe_strike=short_pe - wing,
    )


def build_em_condor_strikes(spot: float, vix: float, short_mult: float = 1.0,
                            wing_mult: float = 1.0, days: int = 7,
                            step: float = _STEP) -> CondorLegs:
    """
    EXPECTED-MOVE condor: strikes placed by implied std-dev instead of a fixed %.
      1 expected move (points) = spot * (vix/100) * sqrt(days/365)
      shorts at short_mult x EM; wings wing_mult x EM further out.
    Mirrors build_condor_strikes but volatility-adaptive.
    """
    em = spot * (vix / 100.0) * (days / 365.0) ** 0.5
    off = max(step, round_to_step(short_mult * em, step))
    wing = max(step, round_to_step(wing_mult * em, step))
    atm = round_to_step(spot, step)
    short_ce = atm + off
    short_pe = atm - off
    return CondorLegs(
        short_ce_strike=short_ce, short_pe_strike=short_pe,
        long_ce_strike=short_ce + wing, long_pe_strike=short_pe - wing,
    )


def entry_credit(premiums: dict) -> float:
    """
    Net credit (points) = (short CE + short PE) - (long CE + long PE).
    premiums keys: 'short_ce','short_pe','long_ce','long_pe' (per-unit option prices).
    """
    return (premiums["short_ce"] + premiums["short_pe"]) - (premiums["long_ce"] + premiums["long_pe"])


def _intrinsic(spot: float, strike: float, is_call: bool) -> float:
    return max(0.0, spot - strike) if is_call else max(0.0, strike - spot)


def expiry_pnl_points(legs: CondorLegs, credit: float, spot_expiry: float) -> float:
    """
    P&L in points at expiry (cash settlement):
      + credit collected
      - short-leg intrinsic paid out
      + long-wing intrinsic received
    Bounded: max profit = credit; max loss = credit - wing_width.
    """
    short_pay = _intrinsic(spot_expiry, legs.short_ce_strike, True) + \
                _intrinsic(spot_expiry, legs.short_pe_strike, False)
    long_recv = _intrinsic(spot_expiry, legs.long_ce_strike, True) + \
                _intrinsic(spot_expiry, legs.long_pe_strike, False)
    return credit - short_pay + long_recv


def condor_costs_points(lot_size: int, cost_per_leg: float = 20.0,
                        slippage_pts_per_leg: float = 1.0, n_legs: int = 4) -> float:
    """Round-trip cost in points (brokerage ₹->pts + slippage), entry+exit legs."""
    brokerage_rs = cost_per_leg * n_legs * 2
    return brokerage_rs / lot_size + slippage_pts_per_leg * n_legs


class PaperCondorBook:
    """Persists paper condor positions to a JSONL and computes running P&L."""

    def __init__(self, path: Path | None = None, lot_size: int = 75) -> None:
        self.path = path or _LOG
        self.lot_size = lot_size
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _write(self, rec: dict) -> None:
        rec = {"ts": datetime.now().isoformat(), **rec}
        with open(self.path, "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")

    def open_condor(self, expiry: str, legs: CondorLegs, premiums: dict, spot: float) -> float:
        credit = entry_credit(premiums)
        self._write({
            "event": "OPEN", "expiry": expiry, "spot": spot,
            "strikes": {"short_ce": legs.short_ce_strike, "short_pe": legs.short_pe_strike,
                        "long_ce": legs.long_ce_strike, "long_pe": legs.long_pe_strike},
            "premiums": premiums, "credit_pts": round(credit, 2),
            "symbols": legs.symbols,
        })
        logger.info(f"[PaperCondor] OPEN {expiry} credit={credit:.1f}pts spot={spot:.0f}")
        return credit

    def settle_condor(self, expiry: str, legs: CondorLegs, credit: float, spot_expiry: float) -> float:
        pnl_pts = expiry_pnl_points(legs, credit, spot_expiry) - \
                  condor_costs_points(self.lot_size)
        pnl_rs = pnl_pts * self.lot_size
        self._write({
            "event": "SETTLE", "expiry": expiry, "spot_expiry": spot_expiry,
            "credit_pts": round(credit, 2), "pnl_pts": round(pnl_pts, 2),
            "pnl_rs": round(pnl_rs, 0),
        })
        logger.info(f"[PaperCondor] SETTLE {expiry} P&L={pnl_pts:.1f}pts (₹{pnl_rs:.0f})")
        return pnl_rs

    def summary(self) -> dict:
        if not self.path.exists():
            return {"trades": 0, "total_pnl_rs": 0.0, "win_rate": 0.0}
        pnls = []
        for line in open(self.path):
            r = json.loads(line)
            if r.get("event") == "SETTLE":
                pnls.append(r["pnl_rs"])
        if not pnls:
            return {"trades": 0, "total_pnl_rs": 0.0, "win_rate": 0.0}
        wins = sum(1 for p in pnls if p > 0)
        return {"trades": len(pnls), "total_pnl_rs": round(sum(pnls), 0),
                "win_rate": round(wins / len(pnls) * 100, 1),
                "avg_rs": round(sum(pnls) / len(pnls), 0)}
