"""
tools/fetch_nse_fo.py

Downloads NSE's historical F&O bhavcopy (real settled option prices, including
EXPIRED contracts) and caches the NIFTY index-option rows per trading day. This
is what lets us backtest the iron condor on REAL prices — Angel's live-only
master can't give expired-contract history, but NSE's public archive can.

Modern UDiFF format (mid-2024 onward):
  https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_YYYYMMDD_F_0000.csv.zip

Resumable: one small CSV per date in data/.cache/nse_fo/, skips existing, and
skips non-trading days (no file on the archive).

Usage:
    python -m tools.fetch_nse_fo --start 2024-08-01 --end 2026-09-25
"""
from __future__ import annotations
import argparse, io, sys, time, zipfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import requests

_OUT = Path(__file__).parent.parent / "data" / ".cache" / "nse_fo"
_HDR = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
_KEEP = ["TradDt", "XpryDt", "StrkPric", "OptnTp", "OpnPric", "HghPric", "LwPric",
         "ClsPric", "SttlmPric", "UndrlygPric", "OpnIntrst", "TtlTradgVol"]


def _session() -> requests.Session:
    s = requests.Session(); s.headers.update(_HDR)
    try:
        s.get("https://www.nseindia.com", timeout=15)  # prime cookies
    except Exception:
        pass
    return s


def _fetch_day(s: requests.Session, d: datetime) -> pd.DataFrame | None:
    ds = d.strftime("%Y%m%d")
    url = f"https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{ds}_F_0000.csv.zip"
    try:
        r = s.get(url, timeout=25)
    except Exception:
        return None
    if r.status_code != 200 or r.content[:2] != b"PK":
        return None  # holiday / not available
    z = zipfile.ZipFile(io.BytesIO(r.content))
    df = pd.read_csv(z.open(z.namelist()[0]))
    nifty = df[(df["TckrSymb"] == "NIFTY") & (df["FinInstrmTp"] == "IDO")]
    cols = [c for c in _KEEP if c in nifty.columns]
    return nifty[cols].copy()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-08-01")
    ap.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"))
    args = ap.parse_args()
    _OUT.mkdir(parents=True, exist_ok=True)

    start = datetime.strptime(args.start, "%Y-%m-%d")
    end = datetime.strptime(args.end, "%Y-%m-%d")
    s = _session()
    d = start
    got = skip = holiday = 0
    while d <= end:
        if d.weekday() < 5:  # Mon-Fri
            path = _OUT / f"{d:%Y-%m-%d}.csv"
            if path.exists():
                skip += 1
            else:
                df = _fetch_day(s, d)
                if df is None or df.empty:
                    holiday += 1
                else:
                    df.to_csv(path, index=False)
                    got += 1
                    if got % 20 == 0:
                        print(f"  {d:%Y-%m-%d}: {len(df)} NIFTY opt rows | got={got} skip={skip} holiday={holiday}", flush=True)
                time.sleep(0.4)  # be gentle
        d += timedelta(days=1)
    print(f"DONE. fetched={got} skipped(existing)={skip} holidays/missing={holiday} -> {_OUT}", flush=True)


if __name__ == "__main__":
    main()
