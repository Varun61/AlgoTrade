"""
Unit tests for strategy/research_intraday.py
(IntradayMomentumStrategy, GapFadeStrategy).
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datetime import datetime, timedelta

import pandas as pd
import pytest

from strategy.research_intraday import IntradayMomentumStrategy, GapFadeStrategy
from strategy.strategy_base import Signal


def _intraday_day(date, opens_closes, start_hm=(9, 15)):
    """Build 15-min bars for one day from a list of (open, close) tuples."""
    rows = []
    t0 = datetime(date.year, date.month, date.day, *start_hm)
    for i, (o, c) in enumerate(opens_closes):
        ts = t0 + timedelta(minutes=15 * i)
        rows.append({"timestamp": ts, "open": o, "high": max(o, c) + 0.2,
                     "low": min(o, c) - 0.2, "close": c, "volume": 10000})
    return rows


def test_momentum_goes_long_after_up_open():
    # First 30 min up ~2%; at 14:00 expect a LONG into the close.
    d = datetime(2024, 6, 3)
    oc = [(100, 101), (101, 102)]                      # first 2 bars: +2%
    oc += [(102, 102)] * 18                            # flat through 14:00 (bar index 19 = 14:00)
    hist = pd.DataFrame(_intraday_day(d, oc))
    strat = IntradayMomentumStrategy(symbol="T", token="1", entry_time="14:00",
                                     min_move_pct=0.3, atr_stop_mult=1.5, atr_period=5, min_price=1)
    sig = strat.on_candle_close(hist.iloc[-1], hist)
    assert sig.signal == Signal.BUY
    assert strat.get_position_direction() == "long"


def test_momentum_goes_short_after_down_open():
    d = datetime(2024, 6, 3)
    oc = [(100, 99), (99, 98)] + [(98, 98)] * 18       # first 30 min -2%
    hist = pd.DataFrame(_intraday_day(d, oc))
    strat = IntradayMomentumStrategy(symbol="T", token="1", entry_time="14:00",
                                     min_move_pct=0.3, atr_stop_mult=1.5, atr_period=5, min_price=1)
    sig = strat.on_candle_close(hist.iloc[-1], hist)
    assert sig.signal == Signal.SELL


def test_momentum_no_trade_before_entry_time():
    d = datetime(2024, 6, 3)
    oc = [(100, 101), (101, 102)] + [(102, 102)] * 6   # only up to ~11:00
    hist = pd.DataFrame(_intraday_day(d, oc))
    strat = IntradayMomentumStrategy(symbol="T", token="1", entry_time="14:00",
                                     min_move_pct=0.3, atr_stop_mult=1.5, atr_period=5, min_price=1)
    assert strat.on_candle_close(hist.iloc[-1], hist).signal == Signal.HOLD


def test_momentum_no_trade_when_move_too_small():
    d = datetime(2024, 6, 3)
    oc = [(100, 100.1), (100.1, 100.05)] + [(100, 100)] * 18  # ~flat open
    hist = pd.DataFrame(_intraday_day(d, oc))
    strat = IntradayMomentumStrategy(symbol="T", token="1", entry_time="14:00",
                                     min_move_pct=0.5, atr_stop_mult=1.5, atr_period=5, min_price=1)
    assert strat.on_candle_close(hist.iloc[-1], hist).signal == Signal.HOLD


def test_gapfade_shorts_a_gap_up():
    d1 = datetime(2024, 6, 3)
    day1 = _intraday_day(d1, [(100, 100)] * 20)            # prior day, close 100
    d2 = datetime(2024, 6, 4)
    day2 = _intraday_day(d2, [(103, 103)])                 # gap up +3% at open, first bar
    hist = pd.DataFrame(day1 + day2)
    strat = GapFadeStrategy(symbol="T", token="1", gap_min_pct=1.0, gap_max_pct=6.0,
                            target_fill=0.5, atr_stop_mult=1.0, atr_period=5,
                            entry_by_time="10:00", min_price=1)
    sig = strat.on_candle_close(hist.iloc[-1], hist)
    assert sig.signal == Signal.SELL
    assert sig.target < sig.entry_price          # fade toward prior close (down)
    assert sig.stop_loss > sig.entry_price


def test_gapfade_longs_a_gap_down():
    d1 = datetime(2024, 6, 3)
    day1 = _intraday_day(d1, [(100, 100)] * 20)
    d2 = datetime(2024, 6, 4)
    day2 = _intraday_day(d2, [(97, 97)])                   # gap down -3%
    hist = pd.DataFrame(day1 + day2)
    strat = GapFadeStrategy(symbol="T", token="1", gap_min_pct=1.0, gap_max_pct=6.0,
                            target_fill=0.5, atr_stop_mult=1.0, atr_period=5,
                            entry_by_time="10:00", min_price=1)
    sig = strat.on_candle_close(hist.iloc[-1], hist)
    assert sig.signal == Signal.BUY
    assert sig.target > sig.entry_price
    assert sig.stop_loss < sig.entry_price


def test_gapfade_skips_small_gap():
    d1 = datetime(2024, 6, 3)
    day1 = _intraday_day(d1, [(100, 100)] * 20)
    d2 = datetime(2024, 6, 4)
    day2 = _intraday_day(d2, [(100.3, 100.3)])             # 0.3% gap < 1% threshold
    hist = pd.DataFrame(day1 + day2)
    strat = GapFadeStrategy(symbol="T", token="1", gap_min_pct=1.0, gap_max_pct=6.0,
                            atr_period=5, entry_by_time="10:00", min_price=1)
    assert strat.on_candle_close(hist.iloc[-1], hist).signal == Signal.HOLD


def test_gapfade_skips_huge_gap():
    d1 = datetime(2024, 6, 3)
    day1 = _intraday_day(d1, [(100, 100)] * 20)
    d2 = datetime(2024, 6, 4)
    day2 = _intraday_day(d2, [(110, 110)])                 # +10% gap > max 6%
    hist = pd.DataFrame(day1 + day2)
    strat = GapFadeStrategy(symbol="T", token="1", gap_min_pct=1.0, gap_max_pct=6.0,
                            atr_period=5, entry_by_time="10:00", min_price=1)
    assert strat.on_candle_close(hist.iloc[-1], hist).signal == Signal.HOLD


from strategy.research_intraday import PrevDayMomentumStrategy


def _two_day_strong_up_close():
    """Prior day rallies and closes at its high (strong up mover); today opens."""
    d1 = datetime(2024, 6, 3)
    # prior day: open 100, drifts up, closes near the high at 105
    oc = [(100, 100.5), (100.5, 101.5), (101.5, 102.5), (102.5, 103.5),
          (103.5, 104.2), (104.2, 104.8), (104.8, 105.0)] + [(105.0, 105.0)] * 13
    day1 = _intraday_day(d1, oc)
    d2 = datetime(2024, 6, 4)
    day2 = _intraday_day(d2, [(105, 105)])  # first bar of next day
    return pd.DataFrame(day1 + day2)


def test_prevday_continuation_longs_strong_up_mover():
    hist = _two_day_strong_up_close()
    strat = PrevDayMomentumStrategy(symbol="T", token="1", up_threshold_pct=2.0,
                                    min_close_strength=0.7, atr_period=5, reversal=False,
                                    entry_by_time="10:00", min_price=1)
    sig = strat.on_candle_close(hist.iloc[-1], hist)
    assert sig.signal == Signal.BUY   # continuation: buy yesterday's strong closer


def test_prevday_reversal_shorts_strong_up_mover():
    hist = _two_day_strong_up_close()
    strat = PrevDayMomentumStrategy(symbol="T", token="1", up_threshold_pct=2.0,
                                    min_close_strength=0.7, atr_period=5, reversal=True,
                                    allow_short=True, entry_by_time="10:00", min_price=1)
    sig = strat.on_candle_close(hist.iloc[-1], hist)
    assert sig.signal == Signal.SELL  # reversal: fade yesterday's strong closer


def test_prevday_skips_quiet_prior_day():
    d1 = datetime(2024, 6, 3)
    day1 = _intraday_day(d1, [(100, 100)] * 20)   # flat prior day, no momentum
    d2 = datetime(2024, 6, 4)
    day2 = _intraday_day(d2, [(100, 100)])
    hist = pd.DataFrame(day1 + day2)
    strat = PrevDayMomentumStrategy(symbol="T", token="1", up_threshold_pct=2.0,
                                    min_close_strength=0.7, atr_period=5, entry_by_time="10:00", min_price=1)
    assert strat.on_candle_close(hist.iloc[-1], hist).signal == Signal.HOLD


from strategy.research_intraday import RegimeSwitchedPrevDayStrategy


def _mk_regsw(**over):
    kw = dict(symbol="T", token="1", up_threshold_pct=2.0, min_close_strength=0.7,
              allow_short=True, vol_mult=0.0, atr_period=5, atr_stop_mult=1.5,
              atr_target_mult=2.0, entry_by_time="14:30", min_price=1)
    kw.update(over)
    return RegimeSwitchedPrevDayStrategy(**kw)


def test_regsw_uptrend_continuation_needs_breakout():
    hist = _two_day_strong_up_close()   # yesterday strong-up close ~105; today first bar at 105
    strat = _mk_regsw()
    strat.set_market_regime(1)          # uptrend -> continuation, but needs break of y-high
    # first bar close == 105 which is the prior-day high, not strictly above -> wait
    sig = strat.on_candle_close(hist.iloc[-1], hist)
    assert sig.signal == Signal.HOLD


def test_regsw_uptrend_longs_on_break_of_prior_high():
    hist = _two_day_strong_up_close()
    # push today's first bar clearly above yesterday's high (~105)
    hist = hist.copy()
    hist.loc[hist.index[-1], ["open", "high", "low", "close"]] = [106, 107, 105.5, 106.5]
    strat = _mk_regsw()
    strat.set_market_regime(1)
    sig = strat.on_candle_close(hist.iloc[-1], hist)
    assert sig.signal == Signal.BUY


def test_regsw_downtrend_fades_up_mover_at_open():
    hist = _two_day_strong_up_close()
    strat = _mk_regsw()
    strat.set_market_regime(-1)         # down/chop -> fade the strong up-mover
    sig = strat.on_candle_close(hist.iloc[-1], hist)
    assert sig.signal == Signal.SELL    # short the up-closer


def test_regime_computation_lookahead_safe_and_signed():
    # Backtester regime: two symbols both trending up -> later days regime +1.
    import pandas as pd
    from backtest.portfolio import PortfolioBacktester, PortfolioConfig
    def mk(base):
        rows = []
        for d in range(30):
            for b in range(3):
                ts = datetime(2024, 1, 1) + timedelta(days=d, minutes=15 * b)
                px = base + d  # steady uptrend day over day
                rows.append({"timestamp": ts, "open": px, "high": px + 0.5,
                             "low": px - 0.5, "close": px, "volume": 1000})
        return pd.DataFrame(rows)
    data = {"1": mk(100), "2": mk(200)}
    bt = PortfolioBacktester([], {}, PortfolioConfig(regime_enabled=True, regime_sma_days=5))
    regime = bt._compute_regime(data)
    # a late date in a persistent uptrend must be +1
    late = sorted(regime.keys())[-1]
    assert regime[late] == 1
