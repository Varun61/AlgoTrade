"""
tools/fetch_history.py

Phase-1 historical data fetcher. Pulls multi-year OHLCV from Angel One for the
full watchlist and caches one CSV per symbol so the backtester can validate
across multiple market regimes (not just the single cached year).

RESUMABLE: writes one file per symbol and skips any that already exist and
already cover the requested start date, so re-running after an interruption
continues where it left off (background jobs in this environment get killed on
new messages, so this matters).

Angel per-request day limits (approx): 1-min=30, 15-min=200, day=2000.
Availability probed: ~5yr of 15-min, ~1yr+ of 1-min.

Usage:
    python -m tools.fetch_history --interval 15 --years 5 --out data/.cache/hist_15m
    python -m tools.fetch_history --interval 1  --years 1 --symbols 50 --out data/.cache/hist_1m
    python -m tools.fetch_history --interval day --years 5 --out data/.cache/hist_1d
"""
from __future__ import annotations
import argparse, logging, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import yaml

from auth.session_manager import SessionManager
from data.historical_fetcher import HistoricalFetcher

logging.basicConfig(level=logging.ERROR, format="%(asctime)s %(levelname)s %(message)s")
SETTINGS = Path(__file__).parent.parent / "config" / "settings.yaml"

_CHUNK = {1: 25, 3: 55, 5: 95, 15: 190, 30: 190, 60: 390, "day": 1800}
_SUFFIX = {1: "1m", 3: "3m", 5: "5m", 15: "15m", 30: "30m", 60: "60m", "day": "1d"}


def _covered(path: Path, start: pd.Timestamp) -> bool:
    """True if the file exists and its earliest row is at/before `start` (already fetched)."""
    if not path.exists():
        return False
    try:
        head = pd.read_csv(path, nrows=1)
        first = pd.to_datetime(head["timestamp"].iloc[0])
        if first.tzinfo is not None:
            first = first.tz_localize(None)
        # allow ~10-day slack (weekends/holidays/listing date)
        return first <= start + pd.Timedelta(days=10)
    except Exception:
        return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", default="15")   # "1","3","5","15","30","60","day"
    ap.add_argument("--years", type=float, default=5.0)
    ap.add_argument("--symbols", type=int, default=None, help="cap number of symbols")
    ap.add_argument("--out", type=str, default=None,
                    help="output dir (default: data/.cache/hist_<suffix>)")
    args = ap.parse_args()

    interval = int(args.interval) if args.interval != "day" else "day"
    total_days = int(args.years * 365)
    chunk_days = _CHUNK[interval]
    suffix = _SUFFIX[interval]
    out = args.out or f"data/.cache/hist_{suffix}"
    out_dir = Path(out); out_dir.mkdir(parents=True, exist_ok=True)
    start = pd.Timestamp.now().normalize() - pd.Timedelta(days=total_days)

    cfg = yaml.safe_load(open(SETTINGS))
    insts = cfg["watchlist"]["instruments"]
    if args.symbols:
        insts = insts[: args.symbols]

    print(f"Fetching {suffix} for {len(insts)} symbols, ~{args.years}yr "
          f"(chunk={chunk_days}d) -> {out_dir}", flush=True)

    sm = SessionManager(); obj = sm.login()
    fetcher = HistoricalFetcher(obj)

    done = skipped = failed = 0
    try:
        for i, inst in enumerate(insts, 1):
            tok = str(inst["token"]); sym = inst["symbol"]; exch = inst["exchange"]
            path = out_dir / f"{tok}_{suffix}.csv"
            if _covered(path, start):
                skipped += 1
                continue
            df = fetcher.fetch_in_chunks(exch, tok, interval, total_days=total_days, chunk_days=chunk_days)
            if df.empty:
                failed += 1
                print(f"  ({i}/{len(insts)}) {sym}: NO DATA", flush=True)
                continue
            df.to_csv(path, index=False)
            done += 1
            if done % 10 == 0 or i == len(insts):
                print(f"  ({i}/{len(insts)}) {sym}: {len(df)} rows "
                      f"[{df['timestamp'].iloc[0]} -> {df['timestamp'].iloc[-1]}] "
                      f"| done={done} skip={skipped} fail={failed}", flush=True)
    finally:
        sm.logout()
    print(f"DONE. fetched={done} skipped(existing)={skipped} failed={failed}", flush=True)


if __name__ == "__main__":
    main()
