"""
data/instrument_master.py

Downloads and caches the Angel One instrument master JSON daily.
The master maps (exchange, symbol) -> token, and is required because:
  - Tokens can change on corporate actions (splits, bonuses)
  - WebSocket subscriptions use tokens, not symbols

Usage:
    python -m data.instrument_master
"""

from __future__ import annotations
import json, logging, time
from datetime import date
from pathlib import Path

import requests
import pandas as pd
import yaml

logger = logging.getLogger(__name__)

_SETTINGS_PATH = Path(__file__).parent.parent / "config" / "settings.yaml"
_CACHE_DIR     = Path(__file__).parent.parent / "data" / ".cache"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _get_master_url() -> str:
    with open(_SETTINGS_PATH) as f:
        cfg = yaml.safe_load(f)
    return cfg["data"]["instrument_master_url"]


def _cache_path_for_today() -> Path:
    return _CACHE_DIR / f"instrument_master_{date.today()}.json"


def download_instrument_master(force: bool = False) -> pd.DataFrame:
    """
    Download (or load from daily cache) the full instrument master.

    Returns a DataFrame with columns:
        token, symbol, name, expiry, strike, lotsize,
        instrumenttype, exch_seg, tick_size
    """
    cache_path = _cache_path_for_today()

    if cache_path.exists() and not force:
        logger.info(f"[InstrumentMaster] Loading from cache: {cache_path.name}")
        with open(cache_path) as f:
            data = json.load(f)
    else:
        url = _get_master_url()
        logger.info(f"[InstrumentMaster] Downloading from {url} ...")
        for attempt in range(3):
            try:
                resp = requests.get(url, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                with open(cache_path, "w") as f:
                    json.dump(data, f)
                logger.info(f"[InstrumentMaster] Downloaded {len(data)} instruments, cached.")
                break
            except Exception as exc:
                logger.warning(f"[InstrumentMaster] Attempt {attempt+1} failed: {exc}")
                time.sleep(2 ** attempt)
        else:
            raise RuntimeError("Instrument master download failed after 3 attempts.")

    df = pd.DataFrame(data)
    # Normalize column names to lowercase
    df.columns = [c.lower().strip() for c in df.columns]
    return df


def get_token(df: pd.DataFrame, symbol: str, exchange: str = "NSE") -> str | None:
    """
    Lookup token for a symbol+exchange combination.

    Args:
        df      : instrument master DataFrame from download_instrument_master()
        symbol  : e.g. "SBIN-EQ"
        exchange: "NSE" | "BSE" | "NFO" etc.

    Returns:
        Token string or None if not found.
    """
    mask = (df["symbol"].str.upper() == symbol.upper()) & \
           (df["exch_seg"].str.upper() == exchange.upper())
    result = df.loc[mask, "token"]
    if result.empty:
        logger.warning(f"[InstrumentMaster] Token not found for {symbol} on {exchange}")
        return None
    return str(result.iloc[0])


def get_symbol(df: pd.DataFrame, token: str) -> tuple[str, str] | None:
    """Reverse lookup: token -> (symbol, exchange)."""
    mask = df["token"].astype(str) == str(token)
    result = df.loc[mask, ["symbol", "exch_seg"]]
    if result.empty:
        return None
    row = result.iloc[0]
    return row["symbol"], row["exch_seg"]


# ------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    df = download_instrument_master()
    print(f"\n✅ Instrument master loaded: {len(df)} rows")
    print(df.head())

    # Quick test
    token = get_token(df, "SBIN-EQ", "NSE")
    print(f"\nSBIN-EQ token: {token}")
