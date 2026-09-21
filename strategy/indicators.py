"""
strategy/indicators.py

Pure-function technical indicators.
All functions take a pd.Series or pd.DataFrame and return a pd.Series.

Rules:
  - No side effects
  - No API calls
  - Vectorized (pandas/numpy) — fast for both live and backtest
"""

from __future__ import annotations
import numpy as np
import pandas as pd


# ------------------------------------------------------------------
# EMA
# ------------------------------------------------------------------

def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average."""
    return series.ewm(span=period, adjust=False).mean()


# ------------------------------------------------------------------
# RSI
# ------------------------------------------------------------------

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """
    Relative Strength Index (Wilder smoothing).
    Returns values in [0, 100].
    """
    delta  = series.diff()
    gain   = delta.clip(lower=0)
    loss   = (-delta).clip(lower=0)

    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


# ------------------------------------------------------------------
# ATR
# ------------------------------------------------------------------

def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average True Range (Wilder smoothing)."""
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()


# ------------------------------------------------------------------
# VWAP (intraday, resets each session)
# ------------------------------------------------------------------

def vwap(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series) -> pd.Series:
    """
    Intraday VWAP — resets each calendar day.

    When history spans multiple days (warm-up data), a naive cumulative sum
    produces a meaningless multi-day VWAP.  This version groups by date so
    the value always reflects only the current session.

    If the index is NOT a DatetimeIndex (e.g. integer-indexed backtest data),
    it falls back to a plain cumulative VWAP.

    Typical price = (H + L + C) / 3
    """
    tp  = (high + low + close) / 3
    tpv = tp * volume

    if isinstance(high.index, pd.DatetimeIndex):
        # Group by calendar date → VWAP resets at midnight / 9:15 open
        dates   = high.index.normalize()   # floor each timestamp to its date
        cum_tpv = tpv.groupby(dates).cumsum()
        cum_vol = volume.groupby(dates).cumsum()
    else:
        # Fallback for integer-indexed synthetic/backtest data
        cum_tpv = tpv.cumsum()
        cum_vol = volume.cumsum()

    return cum_tpv / cum_vol.replace(0, np.nan)



# ------------------------------------------------------------------
# Opening Range (ORB)
# ------------------------------------------------------------------

def opening_range(df: pd.DataFrame, n_candles: int) -> tuple[float, float]:
    """
    Compute the Opening Range: high and low of the first N candles.

    Args:
        df       : DataFrame with 'high', 'low' columns (sorted by time)
        n_candles: Number of opening candles (e.g. 1 for 15-min ORB on 15-min chart)

    Returns:
        (orb_high, orb_low)
    """
    orb_window = df.head(n_candles)
    return float(orb_window["high"].max()), float(orb_window["low"].min())


# ------------------------------------------------------------------
# EMA Crossover signal
# ------------------------------------------------------------------

def ema_crossover(series: pd.Series, fast: int, slow: int) -> pd.Series:
    """
    Returns a Series of crossover signals:
        +1 = fast crossed above slow (bullish)
        -1 = fast crossed below slow (bearish)
         0 = no crossover this candle
    """
    fast_ema  = ema(series, fast)
    slow_ema  = ema(series, slow)
    diff      = fast_ema - slow_ema
    prev_diff = diff.shift(1)

    signal = pd.Series(0, index=series.index)
    signal[diff > 0] = 1
    signal[diff < 0] = -1

    # Only flag the candle where the cross actually happens
    cross = pd.Series(0, index=series.index)
    cross[(diff > 0) & (prev_diff <= 0)] =  1
    cross[(diff < 0) & (prev_diff >= 0)] = -1
    return cross
