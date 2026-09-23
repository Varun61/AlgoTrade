"""
risk/market_regime.py

Market-wide regime gate — inspired by "market timing" strategies (checking the
broad index trend + volatility before trading any single stock at all), on top
of the per-stock ADX/EMA-trend filters that already exist in signal_engine.py.

Rationale: even a good per-stock breakout can fail on a choppy, directionless
market day (see today's ULTRACEMCO/KOTAKBANK/ADANIPORTS whipsaws) — checking
Nifty's own trend strength and India VIX gives one cheap, session-level signal
for "is today a day where breakouts are likely to work at all".

Pure/testable: compute_market_regime() takes plain data, no API calls.
fetch_market_regime_inputs() is the thin, fail-open wrapper that talks to
SmartAPI and is not unit tested (mirrors historical_fetcher's own pattern).
"""

from __future__ import annotations
import logging
from datetime import datetime, timedelta

import pandas as pd

from strategy.indicators import adx

logger = logging.getLogger(__name__)


def compute_market_regime(
    nifty_daily: pd.DataFrame,
    vix_value: float | None,
    adx_period: int = 14,
    min_market_adx: float = 18.0,
    max_vix: float = 20.0,
) -> tuple[bool, str]:
    """
    Decide whether the broad market regime is favorable for a trend-following/
    breakout strategy today.

    Args:
        nifty_daily : DataFrame with columns high, low, close (daily candles,
                      most recent last), needs >= adx_period + 1 rows.
        vix_value   : latest India VIX spot value, or None if unavailable.
        min_market_adx : Nifty's daily ADX must be at or above this — below
                      means the index itself is choppy/non-trending.
        max_vix     : India VIX must be at or below this — above means
                      elevated fear/volatility, prone to whipsaws and gaps.

    Returns:
        (ok, reason) — ok=True means favorable regime, safe to trade normally.
    """
    if nifty_daily is None or len(nifty_daily) < adx_period + 1:
        return True, "Insufficient Nifty history — regime check skipped, defaulting to allow"

    adx_val = adx(nifty_daily["high"], nifty_daily["low"], nifty_daily["close"], adx_period).iloc[-1]
    if pd.isna(adx_val):
        return True, "Nifty ADX unavailable — regime check skipped, defaulting to allow"

    if adx_val < min_market_adx:
        return False, f"Nifty ADX={adx_val:.1f} < {min_market_adx} — choppy/non-trending market"

    if vix_value is not None and vix_value > max_vix:
        return False, f"India VIX={vix_value:.1f} > {max_vix} — elevated volatility/fear regime"

    return True, f"Nifty ADX={adx_val:.1f}, VIX={vix_value if vix_value is not None else 'n/a'} — favorable regime"


def fetch_market_regime_inputs(
    smart_obj,
    instrument_df: pd.DataFrame,
    historical_fetcher,
    lookback_days: int = 40,
) -> tuple[pd.DataFrame | None, float | None]:
    """
    Best-effort fetch of Nifty 50 daily candles + India VIX spot value.
    Fails open (returns None, None) on any lookup/API error so a regime-check
    bug can never accidentally halt the whole bot.
    """
    from data.instrument_master import get_token  # local import avoids a hard dependency at module load

    nifty_daily = None
    vix_value = None

    try:
        nifty_token = get_token(instrument_df, "Nifty 50", "NSE")
        if nifty_token:
            to_date = datetime.now()
            from_date = to_date - timedelta(days=lookback_days * 2)  # pad for weekends/holidays
            nifty_daily = historical_fetcher.fetch("NSE", nifty_token, "day", from_date, to_date)
    except Exception as exc:
        logger.warning(f"[MarketRegime] Failed to fetch Nifty 50 history: {exc}")

    try:
        vix_token = get_token(instrument_df, "India VIX", "NSE")
        if vix_token:
            quote = smart_obj.ltpData("NSE", "India VIX", vix_token)
            if quote and quote.get("status") and quote.get("data"):
                vix_value = float(quote["data"]["ltp"])
    except Exception as exc:
        logger.warning(f"[MarketRegime] Failed to fetch India VIX: {exc}")

    return nifty_daily, vix_value
