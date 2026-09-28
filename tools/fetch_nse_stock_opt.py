"""
tools/fetch_nse_stock_opt.py

Downloads NSE UDiFF F&O bhavcopy and caches STOCK option rows (FinInstrmTp='STO')
for a chosen set of symbols, one CSV per symbol per day, so we can backtest
premium-selling on single stocks and compare vs the NIFTY index.

Only the UDiFF era (2024-07-08 onward) is fetched, because that format carries
UndrlygPric on every row (single stocks aren't in our index price cache). Stock
options are MONTHLY only.

    python -m tools.fetch_nse_stock_opt --symbols RELIANCE,HDFCBANK,ICICIBANK \
        --start 2024-07-08 --end 2026-09-25
"""
from __future__ import annotations
import argparse, io, sys, time, zipfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import pandas as pd
import requests

_OUT = Path(__file__).parent.parent / "data" / ".cache" / "nse_stockopt"
_HDR = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120 Safari/537.36"}
_KEEP = ["TradDt", "XpryDt", "StrkPric", "OptnTp", "OpnPric", "HghPric", "LwPric",
         "ClsPric", "SttlmPric", "UndrlygPric", "OpnIntrst", "TtlTradgVol"]


def _session():
    s = requests.Session(); s.headers.update(_HDR)
    try:
        s.get("https://www.nseindia.com", timeout=15)
    except Exception:
        pass
    return s


def _day(s, d):
    ds = d.strftime("%Y%m%d")
    url = f"https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{ds}_F_0000.csv.zip"
    try:
        r = s.get(url, timeout=25)
    except Exception:
        return None
    if r.status_code != 200 or r.content[:2] != b"PK":
        return None
    z = zipfile.ZipFile(io.BytesIO(r.content))
    return pd.read_csv(z.open(z.namelist()[0]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", required=True, help="comma-separated, e.g. RELIANCE,HDFCBANK")
    ap.add_argument("--start", default="2024-07-08")
    ap.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"))
    args = ap.parse_args()
    syms = [x.strip().upper() for x in args.symbols.split(",")]
    for sym in syms:
        (_OUT / sym).mkdir(parents=True, exist_ok=True)

    s = _session()
    d = datetime.strptime(args.start, "%Y-%m-%d")
    end = datetime.strptime(args.end, "%Y-%m-%d")
    got = skip = miss = 0
    while d <= end:
        if d.weekday() < 5:
            todo = [sym for sym in syms if not (_OUT / sym / f"{d:%Y-%m-%d}.csv").exists()]
            if not todo:
                skip += 1
            else:
                df = _day(s, d)
                if df is None:
                    miss += 1
                else:
                    sto = df[df["FinInstrmTp"] == "STO"]
                    for sym in todo:
                        rows = sto[sto["TckrSymb"] == sym]
                        cols = [c for c in _KEEP if c in rows.columns]
                        rows[cols].to_csv(_OUT / sym / f"{d:%Y-%m-%d}.csv", index=False)
                    got += 1
                    if got % 25 == 0:
                        print(f"  {d:%Y-%m-%d} got={got} skip={skip} miss={miss}", flush=True)
                time.sleep(0.4)
        d += timedelta(days=1)
    print(f"DONE got={got} skip={skip} miss={miss} -> {_OUT}", flush=True)


if __name__ == "__main__":
    main()
