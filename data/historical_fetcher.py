"""
data/historical_fetcher.py

Pulls OHLCV historical candle data from Angel One SmartAPI.
Used for:
  1. Strategy indicator warm-up before market open
  2. Backtesting data collection

Rate limit: ~3 req/sec — enforced via configurable delay + retry.

Usage:
    python -m data.historical_fetcher
"""

from __future__ import annotations
import logging, time
from datetime import datetime, timedelta

import pandas as pd
import yaml
from pathlib import Path

logger = logging.getLogger(__name__)

_SETTINGS_PATH = Path(__file__).parent.parent / "config" / "settings.yaml"

# Angel One accepted interval strings
INTERVAL_MAP = {
    1: "ONE_MINUTE",
    3: "THREE_MINUTE",
    5: "FIVE_MINUTE",
    10: "TEN_MINUTE",
    15: "FIFTEEN_MINUTE",
    30: "THIRTY_MINUTE",
    60: "ONE_HOUR",
    "day": "ONE_DAY",
}


def _load_rate_delay() -> float:
    with open(_SETTINGS_PATH) as f:
        cfg = yaml.safe_load(f)
    return float(cfg["data"].get("historical_rate_limit_delay", 1.0))


# Substrings Angel One returns when the historical-data rate limit is hit.
_RATE_LIMIT_MARKERS = (
    "access denied because of exceeding access rate",
    "exceeding access rate",
    "access rate",
)


def _is_rate_limited(resp) -> bool:
    if not resp:
        return False
    msg = str(resp.get("message", "")).lower()
    errorcode = str(resp.get("errorcode", "")).lower()
    return any(marker in msg for marker in _RATE_LIMIT_MARKERS) or errorcode == "ab1004"


def _is_rate_limited_text(text: str) -> bool:
    # SmartAPI returns a raw (non-JSON) body on rate limit, which surfaces as a
    # JSON-parse exception rather than a structured resp dict — check the text too.
    text = text.lower()
    return any(marker in text for marker in _RATE_LIMIT_MARKERS)


class HistoricalFetcher:
    """
    Fetches OHLCV candle data from SmartAPI getCandleData().

    Args:
        smart_obj: Authenticated SmartConnect instance
    """

    def __init__(self, smart_obj) -> None:
        self.obj = smart_obj
        self._delay = _load_rate_delay()

    def fetch(
        self,
        exchange: str,
        symbol_token: str,
        interval_minutes: int | str,
        from_date: datetime,
        to_date: datetime,
        retries: int = 5,
    ) -> pd.DataFrame:
        """
        Fetch OHLCV candles for a symbol.

        Returns DataFrame with columns:
            timestamp, open, high, low, close, volume
        """
        interval_str = INTERVAL_MAP.get(interval_minutes)
        if not interval_str:
            raise ValueError(f"Unsupported interval: {interval_minutes}. Use {list(INTERVAL_MAP.keys())}")

        params = {
            "exchange":    exchange,
            "symboltoken": str(symbol_token),
            "interval":    interval_str,
            "fromdate":    from_date.strftime("%Y-%m-%d %H:%M"),
            "todate":      to_date.strftime("%Y-%m-%d %H:%M"),
        }

        for attempt in range(1, retries + 1):
            try:
                logger.debug(f"[HistoricalFetcher] Fetching {symbol_token} {interval_str} ...")
                resp = self.obj.getCandleData(params)

                if resp and resp.get("status") and resp["data"]:
                    time.sleep(self._delay)   # Respect rate limit before next call
                    df = pd.DataFrame(
                        resp["data"],
                        columns=["timestamp", "open", "high", "low", "close", "volume"]
                    )
                    df["timestamp"] = pd.to_datetime(df["timestamp"])
                    df = df.sort_values("timestamp").reset_index(drop=True)
                    for col in ["open", "high", "low", "close", "volume"]:
                        df[col] = pd.to_numeric(df[col], errors="coerce")
                    logger.info(f"[HistoricalFetcher] Got {len(df)} candles for token {symbol_token}")
                    return df

                if _is_rate_limited(resp):
                    backoff = max(self._delay, 1.0) * (2 ** attempt)
                    logger.warning(
                        f"[HistoricalFetcher] Rate limited on attempt {attempt} for {symbol_token}, "
                        f"backing off {backoff:.1f}s: {resp}"
                    )
                    time.sleep(backoff)
                    continue

                logger.warning(f"[HistoricalFetcher] Empty/bad response attempt {attempt}: {resp}")

            except Exception as exc:
                if _is_rate_limited_text(str(exc)):
                    backoff = max(self._delay, 1.0) * (2 ** attempt)
                    logger.warning(
                        f"[HistoricalFetcher] Rate limited (exception) on attempt {attempt} for "
                        f"{symbol_token}, backing off {backoff:.1f}s: {exc}"
                    )
                    time.sleep(backoff)
                    continue

                logger.warning(f"[HistoricalFetcher] Attempt {attempt} exception: {exc}")

            time.sleep(max(self._delay, 2 ** attempt))

        logger.error(f"[HistoricalFetcher] Failed after {retries} attempts for {symbol_token}")
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

    def fetch_warmup(
        self,
        exchange: str,
        symbol_token: str,
        interval_minutes: int,
        lookback_days: int = 30,
    ) -> pd.DataFrame:
        """
        Convenience: fetch the last N days of candles for indicator warm-up.
        """
        to_date   = datetime.now()
        from_date = to_date - timedelta(days=lookback_days)
        return self.fetch(exchange, symbol_token, interval_minutes, from_date, to_date)

    def fetch_in_chunks(
        self,
        exchange: str,
        symbol_token: str,
        interval_minutes: int,
        total_days: int = 365,
        chunk_days: int = 30,
    ) -> pd.DataFrame:
        """
        Fetch large date ranges in chunks (SmartAPI limits per-request range).
        Used for backtesting data collection.
        """
        all_chunks = []
        to_date = datetime.now()
        remaining = total_days

        while remaining > 0:
            days_this_chunk = min(chunk_days, remaining)
            from_date = to_date - timedelta(days=days_this_chunk)

            chunk = self.fetch(exchange, symbol_token, interval_minutes, from_date, to_date)
            if not chunk.empty:
                all_chunks.append(chunk)

            to_date   = from_date - timedelta(minutes=interval_minutes)
            remaining -= days_this_chunk

        if not all_chunks:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

        df = pd.concat(all_chunks, ignore_index=True)
        df = df.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
        return df


# ------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent))

    from auth.session_manager import SessionManager
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    sm = SessionManager()
    obj = sm.login()

    fetcher = HistoricalFetcher(obj)
    df = fetcher.fetch_warmup("NSE", "3045", interval_minutes=15, lookback_days=5)
    print(f"\n✅ Fetched {len(df)} candles:")
    print(df.tail(10))

    sm.logout()
