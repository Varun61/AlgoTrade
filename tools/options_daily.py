"""
tools/options_daily.py

ONE command to run once a day. It does everything automatically:
  1. Collects today's real NIFTY option data.
  2. Opens a paper iron-condor if none is open for the current expiry.
  3. Settles any condor whose expiry has passed.
  4. Prints the running paper P&L summary.

Everything is idempotent — safe to run any number of times per day. All defaults
(strikes, wings) are baked in from the backtest, so there is nothing to configure.

Usage (that's the whole thing):
    python -m tools.options_daily
"""
from __future__ import annotations
import logging, sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

# Baked-in config from the backtest (best risk-adjusted, positive every year).
OFFSET_PCT = 2.0
WING_PCT = 1.0
STRIKES_EACH_SIDE = 8      # how many strikes around ATM to collect
CANDLE_INTERVAL = 5


def _hr(title):
    print(f"\n{'='*54}\n {title}\n{'='*54}")


def main() -> None:
    from auth.session_manager import SessionManager
    from data.instrument_master import download_instrument_master
    from data.historical_fetcher import HistoricalFetcher
    from execution.options_paper import PaperCondorBook, build_condor_strikes, CondorLegs
    import tools.run_options_paper as R
    import tools.collect_options as C

    print(f"\nNIFTY options paper run — {datetime.now():%Y-%m-%d %H:%M}")
    sm = SessionManager(); obj = sm.login()
    book = PaperCondorBook(lot_size=R.LOT_SIZE)
    try:
        master = download_instrument_master()
        fetcher = HistoricalFetcher(obj)
        nopt = R._nifty_options(master)
        spot = R._ltp(obj, "NSE", "Nifty 50", R.NIFTY_SPOT_TOKEN)
        future = sorted(e for e in nopt["exp_dt"].dropna().unique()
                        if e >= pd.Timestamp.now().normalize())

        # 1) COLLECT today's real option data around ATM
        _hr("1. Collecting real option data")
        try:
            atm = round(spot / C._STEP) * C._STEP
            strikes = [atm + k * C._STEP for k in range(-STRIKES_EACH_SIDE, STRIKES_EACH_SIDE + 1)]
            exp0 = future[0]
            saved = 0
            from datetime import timedelta
            to_d = datetime.now(); fr_d = to_d - timedelta(days=1)
            sub = nopt[nopt["exp_dt"] == exp0]
            for k in strikes:
                for opt in ("CE", "PE"):
                    row = sub[(sub["strike_rs"] == k) & (sub["symbol"].str.endswith(opt))]
                    if row.empty:
                        continue
                    c = fetcher.fetch("NFO", str(row.iloc[0]["token"]), CANDLE_INTERVAL, fr_d, to_d)
                    if not c.empty:
                        saved += C._append_dedup(C._OUT / f"{row.iloc[0]['symbol']}.csv", c)
            print(f"  collected {saved} new rows into data/.cache/options/")
        except Exception as e:
            print(f"  (collection skipped: {e})")

        # 2) OPEN a paper condor if none open for the nearest expiry
        _hr("2. Opening paper condor (if needed)")
        open_exp = R._open_expiries(book.path)
        exp = future[0]; exp_str = pd.Timestamp(exp).date().isoformat()
        if exp_str in open_exp:
            print(f"  already open for expiry {exp_str} — nothing to do")
        else:
            legs = build_condor_strikes(spot, OFFSET_PCT, WING_PCT)
            prem, ok = {}, True
            for role, strike, opt in [("short_ce", legs.short_ce_strike, "CE"),
                                       ("short_pe", legs.short_pe_strike, "PE"),
                                       ("long_ce", legs.long_ce_strike, "CE"),
                                       ("long_pe", legs.long_pe_strike, "PE")]:
                sym, tok = R._leg(nopt, exp, strike, opt)
                if not sym:
                    print(f"  missing leg {role}@{strike} — skip open"); ok = False; break
                p = R._ltp(obj, "NFO", sym, tok)
                if p is None:
                    print(f"  no price for {sym} — skip open"); ok = False; break
                prem[role] = p; legs.symbols[role] = sym; legs.tokens[role] = tok
            if ok:
                credit = book.open_condor(exp_str, legs, prem, spot)
                print(f"  OPENED {exp_str}: credit {credit:.1f} pts (₹{credit*R.LOT_SIZE:.0f}/lot) | "
                      f"sell {legs.short_pe_strike:.0f}P/{legs.short_ce_strike:.0f}C, "
                      f"wings {legs.long_pe_strike:.0f}/{legs.long_ce_strike:.0f}")

        # 3) SETTLE any expired condors
        _hr("3. Settling expired condors (if any)")
        records = [__import__("json").loads(l) for l in open(book.path)] if book.path.exists() else []
        still_open = R._open_expiries(book.path)
        settled = 0
        now = pd.Timestamp.now()
        for r in records:
            if r.get("event") != "OPEN" or r["expiry"] not in still_open:
                continue
            exp_ts = pd.Timestamp(r["expiry"])
            # Time-safe: only settle once expiry has genuinely passed — a prior day,
            # or expiry-day AFTER the 15:30 close. Never settle pre-close (would use
            # a stale/pre-market spot and book a wrong P&L).
            if exp_ts.date() > now.date():
                continue
            if exp_ts.date() == now.date() and now.time() < datetime.strptime("15:25", "%H:%M").time():
                continue
            s = r["strikes"]
            legs = CondorLegs(s["short_ce"], s["short_pe"], s["long_ce"], s["long_pe"])
            book.settle_condor(r["expiry"], legs, r["credit_pts"], spot)
            settled += 1
        print(f"  settled {settled} condor(s)")

        # 4) STATUS
        _hr("4. Paper P&L so far")
        s = book.summary()
        print(f"  trades={s['trades']}  total=₹{s['total_pnl_rs']:,.0f}  "
              f"win%={s['win_rate']}  avg/trade=₹{s.get('avg_rs',0):,.0f}")
        print(f"\n  Full log: logs/options_paper.jsonl  (paste this file to review)\n")
    finally:
        sm.logout()


if __name__ == "__main__":
    main()
