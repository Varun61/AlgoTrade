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


# UDiFF (new) format started 2024-07-08; older dates use the legacy fo<DDMMMYYYY>bhav
_UDIFF_CUTOVER = datetime(2024, 7, 8)
_IDX_CSV = Path(__file__).parent.parent / "data" / ".cache" / "index" / "nifty_1d.csv"


def _index_close() -> dict:
    """date -> NIFTY close, to fill UndrlygPric for the legacy format (which lacks it)."""
    try:
        df = pd.read_csv(_IDX_CSV)
        df["date"] = pd.to_datetime(df["timestamp"]).dt.tz_localize(None).dt.strftime("%Y-%m-%d")
        return dict(zip(df["date"], df["close"]))
    except Exception:
        return {}


def _fetch_udiff(s: requests.Session, d: datetime) -> pd.DataFrame | None:
    ds = d.strftime("%Y%m%d")
    url = f"https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{ds}_F_0000.csv.zip"
    try:
        r = s.get(url, timeout=25)
    except Exception:
        return None
    if r.status_code != 200 or r.content[:2] != b"PK":
        return None
    z = zipfile.ZipFile(io.BytesIO(r.content))
    df = pd.read_csv(z.open(z.namelist()[0]))
    nifty = df[(df["TckrSymb"] == "NIFTY") & (df["FinInstrmTp"] == "IDO")]
    cols = [c for c in _KEEP if c in nifty.columns]
    return nifty[cols].copy()


def _fetch_legacy(s: requests.Session, d: datetime, idx: dict) -> pd.DataFrame | None:
    """Legacy fo<DD><MON><YYYY>bhav.csv.zip (pre-UDiFF). Maps to the UDiFF schema and
    fills UndrlygPric from the NIFTY index close (legacy files have no underlying)."""
    mon = d.strftime("%b").upper()
    url = (f"https://nsearchives.nseindia.com/content/historical/DERIVATIVES/"
           f"{d.year}/{mon}/fo{d.strftime('%d')}{mon}{d.year}bhav.csv.zip")
    try:
        r = s.get(url, timeout=25)
    except Exception:
        return None
    if r.status_code != 200 or r.content[:2] != b"PK":
        return None
    z = zipfile.ZipFile(io.BytesIO(r.content))
    df = pd.read_csv(z.open(z.namelist()[0]))
    df.columns = [c.strip() for c in df.columns]
    nifty = df[(df["INSTRUMENT"] == "OPTIDX") & (df["SYMBOL"] == "NIFTY")].copy()
    if nifty.empty:
        return None
    trad = pd.to_datetime(nifty["TIMESTAMP"], format="%d-%b-%Y", errors="coerce")
    xpry = pd.to_datetime(nifty["EXPIRY_DT"], format="%d-%b-%Y", errors="coerce")
    out = pd.DataFrame({
        "TradDt": trad.dt.strftime("%Y-%m-%d"),
        "XpryDt": xpry.dt.strftime("%Y-%m-%d"),
        "StrkPric": nifty["STRIKE_PR"].astype(float),
        "OptnTp": nifty["OPTION_TYP"],
        "OpnPric": nifty["OPEN"], "HghPric": nifty["HIGH"],
        "LwPric": nifty["LOW"], "ClsPric": nifty["CLOSE"],
        "SttlmPric": nifty["SETTLE_PR"],
        "UndrlygPric": idx.get(d.strftime("%Y-%m-%d"), float("nan")),
        "OpnIntrst": nifty["OPEN_INT"], "TtlTradgVol": nifty["CONTRACTS"],
    })
    return out


def _fetch_day(s: requests.Session, d: datetime, idx: dict) -> pd.DataFrame | None:
    if d >= _UDIFF_CUTOVER:
        return _fetch_udiff(s, d)
    return _fetch_legacy(s, d, idx)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-08-01")
    ap.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"))
    args = ap.parse_args()
    _OUT.mkdir(parents=True, exist_ok=True)

    start = datetime.strptime(args.start, "%Y-%m-%d")
    end = datetime.strptime(args.end, "%Y-%m-%d")
    s = _session()
    idx = _index_close()
    d = start
    got = skip = holiday = 0
    while d <= end:
        if d.weekday() < 5:  # Mon-Fri
            path = _OUT / f"{d:%Y-%m-%d}.csv"
            if path.exists():
                skip += 1
            else:
                df = _fetch_day(s, d, idx)
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
