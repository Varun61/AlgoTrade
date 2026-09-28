"""
tools/options_daily.py

ONE command to run once a day. Runs the NIFTY weekly condor PAPER system for BOTH
strategy variants in parallel, so we can compare them out-of-sample:

  • weekly     — VALIDATED baseline: fixed 3%-OTM / 2% wings, VIX-timed (>=55th
                 pctile). Log: logs/options_paper.jsonl
  • weekly_em  — EXPERIMENTAL: expected-move (~1 SD) strikes + IV/RV>=1.1 gate.
                 Higher backtest return but bigger tail; under validation.
                 Log: logs/options_paper_em.jsonl

Each day it: (1) collects real option data, (2) opens each variant's condor IF its
gate passes and none is open for the current expiry, (3) settles expired condors
(held to expiry, no stops), (4) prints both variants' running P&L. PAPER only — no
live orders. Expect many "SKIP: gate not met" days; that selectivity IS the edge.
Compare the two with:  python -m tools.compare_paper_backtest

Usage:
    python -m tools.options_daily
"""
from __future__ import annotations
import json, logging, sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

from options.registry import (WEEKLY_SHORT_PCT, WEEKLY_WING_PCT, WEEKLY_VIX_MIN_PCTL,
                              EM_SHORT_MULT, EM_WING_MULT, EM_IVRV_MIN)
from options.vix_filter import vix_percentile

STRIKES_EACH_SIDE = 12
CANDLE_INTERVAL = 5
_LOG_DIR = Path(__file__).parent.parent / "logs"


def _hr(title):
    print(f"\n{'='*54}\n {title}\n{'='*54}")


def _price_legs(R, obj, nopt, exp, legs):
    """Fetch real LTPs for the 4 condor legs; returns (premiums, ok)."""
    prem = {}
    for role, strike, opt in [("short_ce", legs.short_ce_strike, "CE"),
                               ("short_pe", legs.short_pe_strike, "PE"),
                               ("long_ce", legs.long_ce_strike, "CE"),
                               ("long_pe", legs.long_pe_strike, "PE")]:
        sym, tok = R._leg(nopt, exp, strike, opt)
        if not sym:
            print(f"    missing leg {role}@{strike} — skip"); return prem, False
        p = R._ltp(obj, "NFO", sym, tok)
        if p is None:
            print(f"    no price for {sym} — skip"); return prem, False
        prem[role] = p; legs.symbols[role] = sym; legs.tokens[role] = tok
    return prem, True


def _open_variant(name, book, R, obj, nopt, exp, exp_str, legs, gate_ok, gate_msg):
    if exp_str in R._open_expiries(book.path):
        print(f"  [{name}] already open for {exp_str}"); return
    if not gate_ok:
        print(f"  [{name}] SKIP: {gate_msg}"); return
    prem, ok = _price_legs(R, obj, nopt, exp, legs)
    if not ok:
        return
    credit = book.open_condor(exp_str, legs, prem, R._ltp(obj, "NSE", "Nifty 50", R.NIFTY_SPOT_TOKEN))
    print(f"  [{name}] OPENED {exp_str}: credit {credit:.1f} pts | "
          f"sell {legs.short_pe_strike:.0f}P/{legs.short_ce_strike:.0f}C, "
          f"wings {legs.long_pe_strike:.0f}/{legs.long_ce_strike:.0f}")


def _settle_variant(name, book, spot, CondorLegs):
    records = [json.loads(l) for l in open(book.path)] if book.path.exists() else []
    still_open = _open_expiries_local(book.path)
    now = pd.Timestamp.now(); settled = 0
    for r in records:
        if r.get("event") != "OPEN" or r["expiry"] not in still_open:
            continue
        exp_ts = pd.Timestamp(r["expiry"])
        if exp_ts.date() > now.date():
            continue
        # F&O trades to 3:40 PM (CAS regime) — only settle after the close.
        if exp_ts.date() == now.date() and now.time() < datetime.strptime("15:40", "%H:%M").time():
            continue
        s = r["strikes"]
        legs = CondorLegs(s["short_ce"], s["short_pe"], s["long_ce"], s["long_pe"])
        book.settle_condor(r["expiry"], legs, r["credit_pts"], spot)
        settled += 1
    print(f"  [{name}] settled {settled}")


def _open_expiries_local(path):
    opened, settled = set(), set()
    if path.exists():
        for line in open(path):
            r = json.loads(line)
            if r.get("event") == "OPEN": opened.add(r["expiry"])
            if r.get("event") == "SETTLE": settled.add(r["expiry"])
    return opened - settled


def main() -> None:
    from auth.session_manager import SessionManager
    from data.instrument_master import download_instrument_master
    from data.historical_fetcher import HistoricalFetcher
    from execution.options_paper import (PaperCondorBook, build_condor_strikes,
                                         build_em_condor_strikes, CondorLegs)
    import tools.run_options_paper as R
    import tools.collect_options as C

    print(f"\nNIFTY options paper run — {datetime.now():%Y-%m-%d %H:%M}")
    sm = SessionManager(); obj = sm.login()
    book_fixed = PaperCondorBook(path=_LOG_DIR / "options_paper.jsonl", lot_size=R.LOT_SIZE)
    book_em = PaperCondorBook(path=_LOG_DIR / "options_paper_em.jsonl", lot_size=R.LOT_SIZE)
    try:
        master = download_instrument_master()
        fetcher = HistoricalFetcher(obj)
        nopt = R._nifty_options(master)
        spot = R._ltp(obj, "NSE", "Nifty 50", R.NIFTY_SPOT_TOKEN)
        future = sorted(e for e in nopt["exp_dt"].dropna().unique()
                        if e >= pd.Timestamp.now().normalize())
        exp = future[0]; exp_str = pd.Timestamp(exp).date().isoformat()

        # --- volatility signals (shared) ---
        vseries = R.update_and_get_vix(obj)
        vix_latest = vseries[-1] if vseries else None
        vpct = vix_percentile(vseries)
        nifty_closes = R.update_and_get_nifty(obj, spot)
        iv_rv = R.compute_iv_rv(vix_latest, nifty_closes) if vix_latest else 1.0
        print(f"  signals: spot={spot} VIX={vix_latest} VIX%ile={vpct} IV/RV={iv_rv:.2f}")

        # 1) COLLECT real option data around ATM
        _hr("1. Collecting real option data")
        try:
            atm = round(spot / C._STEP) * C._STEP
            strikes = [atm + k * C._STEP for k in range(-STRIKES_EACH_SIDE, STRIKES_EACH_SIDE + 1)]
            to_d = datetime.now(); fr_d = to_d - timedelta(days=1)
            sub = nopt[nopt["exp_dt"] == exp]; saved = 0
            for k in strikes:
                for opt in ("CE", "PE"):
                    row = sub[(sub["strike_rs"] == k) & (sub["symbol"].str.endswith(opt))]
                    if row.empty:
                        continue
                    c = fetcher.fetch("NFO", str(row.iloc[0]["token"]), CANDLE_INTERVAL, fr_d, to_d)
                    if not c.empty:
                        saved += C._append_dedup(C._OUT / f"{row.iloc[0]['symbol']}.csv", c)
            print(f"  collected {saved} new rows")
        except Exception as e:
            print(f"  (collection skipped: {e})")

        # 2) OPEN each variant if its gate passes
        _hr("2. Opening paper condors (if gates pass)")
        vix_ok = vpct is not None and vpct >= WEEKLY_VIX_MIN_PCTL
        _open_variant("weekly", book_fixed, R, obj, nopt, exp, exp_str,
                      build_condor_strikes(spot, WEEKLY_SHORT_PCT, WEEKLY_WING_PCT),
                      vix_ok, f"VIX %ile {vpct} < {WEEKLY_VIX_MIN_PCTL:.0f}")
        em_ok = vix_ok and iv_rv >= EM_IVRV_MIN
        em_msg = f"VIX %ile {vpct} < {WEEKLY_VIX_MIN_PCTL:.0f}" if not vix_ok else f"IV/RV {iv_rv:.2f} < {EM_IVRV_MIN}"
        if vix_latest:
            _open_variant("weekly_em", book_em, R, obj, nopt, exp, exp_str,
                          build_em_condor_strikes(spot, vix_latest, EM_SHORT_MULT, EM_WING_MULT),
                          em_ok, em_msg)

        # 3) SETTLE expired condors for both variants
        _hr("3. Settling expired condors")
        _settle_variant("weekly", book_fixed, spot, CondorLegs)
        _settle_variant("weekly_em", book_em, spot, CondorLegs)

        # 4) STATUS
        _hr("4. Paper P&L so far")
        for name, bk in [("weekly", book_fixed), ("weekly_em", book_em)]:
            s = bk.summary()
            print(f"  {name:<10} trades={s['trades']}  total=₹{s['total_pnl_rs']:,.0f}  "
                  f"win%={s['win_rate']}  avg=₹{s.get('avg_rs',0):,.0f}")
        print(f"\n  Logs: logs/options_paper.jsonl + logs/options_paper_em.jsonl")
        print(f"  Compare vs backtest:  python -m tools.compare_paper_backtest\n")
    finally:
        sm.logout()


if __name__ == "__main__":
    main()
