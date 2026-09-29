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

from options.registry import WEEKLY_SHORT_PCT, WEEKLY_WING_PCT, LOT_SIZE as _LOT

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
NIFTY_SPOT_TOKEN = "99926000"
INDIA_VIX_TOKEN = "99926017"   # Angel token for India VIX (NSE index)
LOT_SIZE = _LOT                 # 75 (single source of truth: options/registry.py)


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


_VIX_CSV = Path(__file__).parent.parent / "data" / ".cache" / "index" / "vix_1d.csv"


_MIN_HISTORY = 60   # never treat fewer cached rows than this as valid history


def _load_series(csv_path):
    """Load a cached daily series (timestamp,close,...) as a tz-normalized DataFrame.
    Returns None if the file is missing or has too little history — callers must
    treat None as a DATA ERROR (not a reason to trade blind or overwrite the cache)."""
    if not csv_path.exists():
        print(f"  WARNING: {csv_path.name} missing — cannot compute vol gates."); return None
    df = pd.read_csv(csv_path)
    ts = pd.to_datetime(df["timestamp"], format="mixed", errors="coerce", utc=True)
    df = df.assign(ts=ts.dt.tz_convert("Asia/Kolkata")).dropna(subset=["ts"]).sort_values("ts")
    if len(df) < _MIN_HISTORY:
        print(f"  WARNING: {csv_path.name} has only {len(df)} valid rows (<{_MIN_HISTORY}) — "
              f"history looks corrupted; NOT using and NOT overwriting it."); return None
    return df


def update_and_get_vix(obj) -> list[float]:
    """Append today's live India VIX to the cached daily series (dedup by date) so
    the 60-day percentile stays current, and return the list of daily closes.
    Returns [] (not a truncated series) if the cache is missing/corrupt, so the
    caller fails LOUD instead of skipping forever or overwriting good history."""
    df = _load_series(_VIX_CSV)
    if df is None:
        return []
    today = pd.Timestamp(datetime.now().date(), tz="Asia/Kolkata")
    live = _ltp(obj, "NSE", "India VIX", INDIA_VIX_TOKEN)
    if live is not None and today.date() not in set(df["ts"].dt.date):
        new = pd.DataFrame([{"close": live, "ts": today}])
        df = pd.concat([df[["close", "ts"]], new], ignore_index=True).sort_values("ts")
        out = df.copy()
        out["timestamp"] = out["ts"].dt.strftime("%Y-%m-%d %H:%M:%S%z")
        out[["timestamp", "close"]].to_csv(_VIX_CSV, index=False)  # safe: df >= _MIN_HISTORY
    closes = df["close"].dropna().tolist()
    if live is not None and (not closes or closes[-1] != live):
        closes.append(live)
    return closes


_NIFTY_CSV = Path(__file__).parent.parent / "data" / ".cache" / "index" / "nifty_1d.csv"


def update_and_get_nifty(obj, spot: float | None = None) -> list[float]:
    """Append today's NIFTY close to the cached daily series (dedup by date) and
    return the list of daily closes — used to compute realized volatility.
    Returns [] if the cache is missing/corrupt (fail loud, don't overwrite)."""
    df = _load_series(_NIFTY_CSV)
    if df is None:
        return []
    if spot is None:
        spot = _ltp(obj, "NSE", "Nifty 50", NIFTY_SPOT_TOKEN)
    today = pd.Timestamp(datetime.now().date(), tz="Asia/Kolkata")
    if spot is not None and today.date() not in set(df["ts"].dt.date):
        new = pd.DataFrame([{"close": spot, "ts": today}])
        df = pd.concat([df[["close", "ts"]], new], ignore_index=True).sort_values("ts")
        out = df.copy(); out["timestamp"] = out["ts"].dt.strftime("%Y-%m-%d %H:%M:%S%z")
        out[["timestamp", "close"]].to_csv(_NIFTY_CSV, index=False)
    closes = df["close"].dropna().tolist()
    if spot is not None and (not closes or closes[-1] != spot):
        closes.append(spot)
    return closes


def compute_iv_rv(vix_latest: float, nifty_closes: list[float], days: int = 10) -> float:
    """VIX / realized-vol ratio (realized = std of last `days` daily returns, annualized)."""
    if len(nifty_closes) < days + 1:
        return 1.0
    import numpy as np
    arr = np.array(nifty_closes[-(days + 1):], dtype=float)
    rets = np.diff(arr) / arr[:-1]
    rv = rets.std() * np.sqrt(252) * 100.0
    return (vix_latest / rv) if rv > 0 else 1.0


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
    ap.add_argument("--offset-pct", type=float, default=WEEKLY_SHORT_PCT)
    ap.add_argument("--wing-pct", type=float, default=WEEKLY_WING_PCT)
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
            # VIX GATE — the edge is in only selling when premium is rich.
            from options.vix_filter import should_sell
            sell, pctl = should_sell(update_and_get_vix(obj))
            if not sell:
                print(f"SKIP: VIX 60d-percentile = {pctl} (< threshold). Premium too "
                      f"thin — sitting out this week (this discipline IS the edge).")
                return
            print(f"VIX gate OK: 60d-percentile = {pctl}. Selling the condor.")
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
