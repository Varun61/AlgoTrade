"""
tools/run_options_paper.py

Paper-mode iron-condor runner for NIFTY weekly options. NO live orders — records
paper entries at REAL premiums (live LTP) and settles at expiry against the real
NIFTY close, logging to logs/options_paper.jsonl. This is how we earn confidence
in the condor edge on REAL prices before any live trading.

Actions:
  open    : build the condor for the nearest expiry, fetch real premiums, log a
            paper OPEN (skips if that expiry is already open).
  settle  : for any open expiry that has reached/passed expiry, fetch the NIFTY
            settlement and log the paper P&L.
  status  : print the running paper P&L summary.

Typical cron: `open` on the first trading day of the week (e.g. Mon 09:30),
`settle` on expiry day after close (e.g. Tue/Thu 15:35). Safe to run repeatedly.

Usage:
    python -m tools.run_options_paper --action open   --offset-pct 2.0 --wing-pct 1.0
    python -m tools.run_options_paper --action settle
    python -m tools.run_options_paper --action status
"""
from __future__ import annotations
import argparse, json, logging, sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd

from execution.options_paper import (
    PaperCondorBook, build_condor_strikes, CondorLegs, entry_credit,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
NIFTY_SPOT_TOKEN = "99926000"
LOT_SIZE = 65


def _nifty_options(master):
    n = master[(master["exch_seg"] == "NFO") & (master["name"] == "NIFTY") &
               (master["instrumenttype"] == "OPTIDX")].copy()
    n["exp_dt"] = pd.to_datetime(n["expiry"], format="%d%b%Y", errors="coerce")
    n["strike_rs"] = n["strike"].astype(float) / 100.0
    return n


def _leg(nopt, expiry_dt, strike, opt):
    row = nopt[(nopt["exp_dt"] == expiry_dt) & (nopt["strike_rs"] == strike) &
               (nopt["symbol"].str.endswith(opt))]
    if row.empty:
        return None, None
    return row.iloc[0]["symbol"], str(row.iloc[0]["token"])


def _ltp(obj, exch, symbol, token):
    q = obj.ltpData(exch, symbol, token)
    if q and q.get("data"):
        return float(q["data"]["ltp"])
    return None


def _open_expiries(book_path: Path) -> set:
    opened, settled = set(), set()
    if book_path.exists():
        for line in open(book_path):
            r = json.loads(line)
            if r.get("event") == "OPEN": opened.add(r["expiry"])
            if r.get("event") == "SETTLE": settled.add(r["expiry"])
    return opened - settled


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--action", choices=["open", "settle", "status"], default="status")
    ap.add_argument("--offset-pct", type=float, default=2.0)
    ap.add_argument("--wing-pct", type=float, default=1.0)
    args = ap.parse_args()

    book = PaperCondorBook(lot_size=LOT_SIZE)

    if args.action == "status":
        print("Paper condor summary:", book.summary())
        return

    from auth.session_manager import SessionManager
    from data.instrument_master import download_instrument_master
    sm = SessionManager(); obj = sm.login()
    try:
        master = download_instrument_master()
        nopt = _nifty_options(master)
        future = sorted(e for e in nopt["exp_dt"].dropna().unique()
                        if e >= pd.Timestamp.now().normalize())
        spot = _ltp(obj, "NSE", "Nifty 50", NIFTY_SPOT_TOKEN)

        if args.action == "open":
            if not future:
                print("No future expiry found."); return
            exp = future[0]; exp_str = pd.Timestamp(exp).date().isoformat()
            if exp_str in _open_expiries(book.path):
                print(f"Expiry {exp_str} already open — skipping."); return
            legs = build_condor_strikes(spot, args.offset_pct, args.wing_pct)
            prem = {}
            for role, strike, opt in [("short_ce", legs.short_ce_strike, "CE"),
                                       ("short_pe", legs.short_pe_strike, "PE"),
                                       ("long_ce", legs.long_ce_strike, "CE"),
                                       ("long_pe", legs.long_pe_strike, "PE")]:
                sym, tok = _leg(nopt, exp, strike, opt)
                if not sym:
                    print(f"Missing leg {role} @ {strike} — aborting open."); return
                p = _ltp(obj, "NFO", sym, tok)
                if p is None:
                    print(f"No LTP for {sym} — aborting open."); return
                prem[role] = p; legs.symbols[role] = sym; legs.tokens[role] = tok
            credit = book.open_condor(exp_str, legs, prem, spot)
            print(f"Opened paper condor {exp_str}: credit={credit:.1f} pts "
                  f"(₹{credit*LOT_SIZE:.0f}/lot). Strikes {legs.short_pe_strike:.0f}/"
                  f"{legs.short_ce_strike:.0f}, wings {legs.long_pe_strike:.0f}/{legs.long_ce_strike:.0f}")

        elif args.action == "settle":
            open_exp = _open_expiries(book.path)
            settled_any = False
            # rebuild legs+credit from the OPEN records
            records = [json.loads(l) for l in open(book.path)] if book.path.exists() else []
            for r in records:
                if r.get("event") != "OPEN":
                    continue
                exp_str = r["expiry"]
                if exp_str not in open_exp:
                    continue
                if pd.Timestamp(exp_str) > pd.Timestamp.now().normalize():
                    continue  # not expired yet
                s = r["strikes"]
                legs = CondorLegs(s["short_ce"], s["short_pe"], s["long_ce"], s["long_pe"])
                credit = r["credit_pts"]
                book.settle_condor(exp_str, legs, credit, spot)  # spot ~ expiry close if run after close
                settled_any = True
            print("Settled." if settled_any else "Nothing to settle yet.")
    finally:
        sm.logout()
    print("Summary:", book.summary())


if __name__ == "__main__":
    main()
