"""
tools/collect_options.py

Real-options FORWARD collector. Angel's instrument master is live-only (expired
option tokens vanish), so a real historical options series can't be pulled after
the fact. This captures it going forward: run it daily (after close, or intraday),
and it appends each day's real option candles for NIFTY strikes around ATM to a
per-contract cache. Over a few weeks this builds the REAL dataset needed to
validate the iron-condor edge (the synthetic BS+VIX backtest is only directional).

For the nearest N weekly expiries it grabs ATM +/- `strikes` (both CE & PE) and
appends today's (or a date range's) candles, de-duplicated by timestamp so
re-running is safe/idempotent.

Usage:
    python -m tools.collect_options                      # nearest expiry, ATM+/-6, today
    python -m tools.collect_options --strikes 8 --expiries 2 --interval 5
    python -m tools.collect_options --days 5             # backfill last 5 days (from now)
"""
from __future__ import annotations
import argparse, logging, sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd

from auth.session_manager import SessionManager
from data.instrument_master import download_instrument_master
from data.historical_fetcher import HistoricalFetcher

logging.basicConfig(level=logging.ERROR, format="%(asctime)s %(levelname)s %(message)s")
_OUT = Path(__file__).parent.parent / "data" / ".cache" / "options"
_STEP = 50.0
NIFTY_SPOT_TOKEN = "99926000"


def _append_dedup(path: Path, df: pd.DataFrame) -> int:
    """Append rows, de-duplicating by timestamp. Returns new-row count."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        old = pd.read_csv(path)
        combined = pd.concat([old, df], ignore_index=True)
        combined["timestamp"] = pd.to_datetime(combined["timestamp"])
        combined = combined.drop_duplicates("timestamp").sort_values("timestamp")
        new_rows = len(combined) - len(old)
        combined.to_csv(path, index=False)
        return max(0, new_rows)
    df.to_csv(path, index=False)
    return len(df)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strikes", type=int, default=6, help="ATM +/- this many strikes each side")
    ap.add_argument("--expiries", type=int, default=1, help="nearest N weekly expiries")
    ap.add_argument("--interval", type=int, default=5, help="candle minutes")
    ap.add_argument("--days", type=int, default=1, help="lookback window to fetch (from now)")
    args = ap.parse_args()

    sm = SessionManager(); obj = sm.login()
    fetcher = HistoricalFetcher(obj)
    df = download_instrument_master()
    n = df[(df["exch_seg"] == "NFO") & (df["name"] == "NIFTY") & (df["instrumenttype"] == "OPTIDX")].copy()
    n["exp_dt"] = pd.to_datetime(n["expiry"], format="%d%b%Y", errors="coerce")
    n["strike_rs"] = n["strike"].astype(float) / 100.0

    # NIFTY spot -> ATM
    q = obj.ltpData("NSE", "Nifty 50", NIFTY_SPOT_TOKEN)
    spot = float(q["data"]["ltp"]) if q and q.get("data") else None
    if not spot:
        print("Could not get NIFTY spot; aborting."); sm.logout(); return
    atm = round(spot / _STEP) * _STEP
    strikes = [atm + k * _STEP for k in range(-args.strikes, args.strikes + 1)]

    expiries = sorted(e for e in n["exp_dt"].dropna().unique() if e >= pd.Timestamp.now().normalize())[: args.expiries]
    to_date = datetime.now(); from_date = to_date - timedelta(days=args.days)

    print(f"NIFTY spot={spot:.1f} ATM={atm:.0f} | strikes {strikes[0]:.0f}-{strikes[-1]:.0f} "
          f"| expiries={[pd.Timestamp(e).date() for e in expiries]}", flush=True)

    saved = 0
    for exp in expiries:
        sub = n[n["exp_dt"] == exp]
        for k in strikes:
            for opt in ("CE", "PE"):
                row = sub[(sub["strike_rs"] == k) & (sub["symbol"].str.endswith(opt))]
                if row.empty:
                    continue
                tok = str(row.iloc[0]["token"]); sym = row.iloc[0]["symbol"]
                cndl = fetcher.fetch("NFO", tok, args.interval, from_date, to_date)
                if cndl.empty:
                    continue
                added = _append_dedup(_OUT / f"{sym}.csv", cndl)
                saved += added
    sm.logout()
    print(f"DONE. appended {saved} new rows across {len(list(_OUT.glob('*.csv')))} contract files in {_OUT}", flush=True)


if __name__ == "__main__":
    main()
