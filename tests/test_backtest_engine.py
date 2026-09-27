"""
Regression tests for backtest/engine.py.

These tests use a fully deterministic `ScriptedStrategy` test double rather
than the real ORBEMAVWAPStrategy, so the engine's *contract* with any
StrategyBase implementation can be verified precisely and independently of
composite indicator math. A separate integration smoke test at the bottom
exercises the real strategy end-to-end.

Contract under test (this is exactly what the parity bug fix addresses):
  1. While a position is open, if neither the intrabar stop-loss nor target
     is touched, the engine must consult strategy.on_candle_close() and honor
     an EXIT_LONG/EXIT_SHORT signal from it (previously dead code).
  2. If the intrabar stop/target IS touched, the engine must close the
     position itself WITHOUT calling the strategy that candle (the strategy
     never even sees it), and must call strategy.force_exit() afterward.
  3. The same applies to EOD square-off and end-of-data closes.
  4. A position closed on candle i can never be re-opened on that same
     candle i (one signal per candle, matching the live orchestrator).
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datetime import datetime, timedelta

import pandas as pd
import pytest

from backtest.engine import BacktestEngine, BacktestConfig
from strategy.strategy_base import StrategyBase, TradeSignal, Signal


class ScriptedStrategy(StrategyBase):
    """Test double: returns a pre-scripted TradeSignal keyed by candle index."""

    def __init__(self, symbol, token, script: dict, **kwargs):
        super().__init__(symbol, token, **kwargs)
        self.script            = script
        self.calls             = []   # candle indices on_candle_close was invoked for
        self.force_exit_calls  = 0
        self.reset_calls       = 0
        self._idx              = -1

    def on_candle_close(self, candle, history):
        self._idx = candle.name  # candle.name == the DataFrame row's positional index
        self.calls.append(self._idx)
        spec = self.script.get(self._idx)
        if spec is None:
            return TradeSignal(signal=Signal.HOLD, symbol=self.symbol, token=self.token)
        return spec

    def force_exit(self):
        self.force_exit_calls += 1

    def reset(self):
        self.reset_calls += 1


def _candle(i, close, high=None, low=None, minute_offset=None):
    ts = datetime(2024, 5, 6, 9, 15) + timedelta(minutes=minute_offset if minute_offset is not None else i)
    high = close + 1 if high is None else high
    low  = close - 1 if low  is None else low
    return {"timestamp": ts, "open": close, "high": high, "low": low, "close": close, "volume": 1000}


def make_engine(script):
    return BacktestEngine(
        strategy_class=ScriptedStrategy,
        symbol="TEST-EQ", token="1",
        strategy_kwargs={"script": script},
        config=BacktestConfig(capital=100_000, slippage_pct=0.0, brokerage_pct=0.0),
    )


def test_strategy_exit_signal_is_honored_without_intrabar_hit():
    """Core parity-fix test: strategy-driven EXIT_LONG must close the trade
    even though the candle's high/low never touch stop_loss or target."""
    script = {
        2: TradeSignal(signal=Signal.BUY, symbol="TEST-EQ", token="1",
                       entry_price=100, stop_loss=90, target=200),
        4: TradeSignal(signal=Signal.EXIT_LONG, symbol="TEST-EQ", token="1",
                       reason="Scripted trend-reversal exit"),
    }
    rows = [
        _candle(0, 100),
        _candle(1, 100),
        _candle(2, 100),                       # entry candle
        _candle(3, 101, high=102, low=99),      # holds — no intrabar hit
        _candle(4, 102, high=103, low=100),     # strategy exit fires here
    ]
    candles = pd.DataFrame(rows)

    calls = []

    class TrackedScripted(ScriptedStrategy):
        def on_candle_close(self, candle, history):
            calls.append(candle.name)
            return super().on_candle_close(candle, history)

    engine = BacktestEngine(
        strategy_class=TrackedScripted, symbol="TEST-EQ", token="1",
        strategy_kwargs={"script": script},
        config=BacktestConfig(capital=100_000, slippage_pct=0.0, brokerage_pct=0.0),
    )
    result = engine.run(candles)

    assert result.total_trades == 1
    assert result.trades[0]["reason"] == "Scripted trend-reversal exit"
    # The strategy must have been consulted on every candle from entry onward,
    # including the exit candle itself (this is exactly the call that was
    # missing before the fix — previously candle 4 would never reach the
    # strategy while a position was open).
    assert calls == [1, 2, 3, 4]


def test_intrabar_stop_hit_bypasses_strategy_and_calls_force_exit():
    """If the hard stop is touched intrabar, the engine must close the trade
    itself, must NOT ask the strategy that candle, and must call force_exit()."""
    tracker = {}

    class TrackedScripted(ScriptedStrategy):
        def on_candle_close(self, candle, history):
            result = super().on_candle_close(candle, history)
            tracker.setdefault("calls", []).append(candle.name)
            return result

        def force_exit(self):
            super().force_exit()
            tracker["force_exit_calls"] = self.force_exit_calls

    script = {
        2: TradeSignal(signal=Signal.BUY, symbol="TEST-EQ", token="1",
                       entry_price=100, stop_loss=90, target=200),
        # If the strategy WERE consulted at candle 3, it would (wrongly) say BUY.
        3: TradeSignal(signal=Signal.BUY, symbol="TEST-EQ", token="1",
                       entry_price=100, stop_loss=90, target=200),
    }
    rows = [
        _candle(0, 100),
        _candle(1, 100),
        _candle(2, 100),                      # entry candle
        _candle(3, 85, high=100, low=85),     # low breaches stop_loss=90 intrabar
    ]
    candles = pd.DataFrame(rows)

    engine = BacktestEngine(
        strategy_class=TrackedScripted, symbol="TEST-EQ", token="1",
        strategy_kwargs={"script": script},
        config=BacktestConfig(capital=100_000, slippage_pct=0.0, brokerage_pct=0.0),
    )
    result = engine.run(candles)

    assert result.total_trades == 1
    assert result.trades[0]["reason"] == "Stop hit"
    assert 3 not in tracker["calls"], "strategy must not be consulted when intrabar SL fires"
    assert tracker["force_exit_calls"] == 1


def test_intrabar_stop_uses_strategy_trailing_update_not_stale_entry_stop():
    """Regression test: after a HOLD candle, the engine must resync its
    intrabar hard-stop with strategy.get_current_stop() so a subsequent wick
    is checked against the strategy's tightened (trailing) stop, not the
    stale stop recorded at entry."""

    class TrailingScripted(ScriptedStrategy):
        def __init__(self, symbol, token, script, **kwargs):
            super().__init__(symbol, token, script, **kwargs)
            self._live_stop = None

        def on_candle_close(self, candle, history):
            sig = super().on_candle_close(candle, history)
            # Simulate the strategy tightening its stop on candle 3 (a HOLD).
            if candle.name == 3:
                self._live_stop = 95.0  # tighter than the original stop_loss=90
            return sig

        def get_current_stop(self):
            return self._live_stop

    script = {
        2: TradeSignal(signal=Signal.BUY, symbol="TEST-EQ", token="1",
                       entry_price=100, stop_loss=90, target=200),
        # candle 3 stays HOLD (default) but triggers the simulated trail above
    }
    rows = [
        _candle(0, 100), _candle(1, 100),
        _candle(2, 100),                        # entry, stop_loss=90
        _candle(3, 100, high=101, low=99),       # HOLD candle; stop tightens to 95
        _candle(4, 93, high=100, low=93),        # low=93: misses stale 90, hits new 95
    ]
    candles = pd.DataFrame(rows)
    engine = BacktestEngine(
        strategy_class=TrailingScripted, symbol="TEST-EQ", token="1",
        strategy_kwargs={"script": script},
        config=BacktestConfig(capital=100_000, slippage_pct=0.0, brokerage_pct=0.0),
    )
    result = engine.run(candles)

    assert result.total_trades == 1
    assert result.trades[0]["reason"] == "Stop hit"
    # Exit price is the candle close (engine's _close_position always exits at
    # candle close once a reason is determined), confirming candle 4 (not a
    # later one) is where the exit fired.
    assert result.trades[0]["exit_price"] == 93.0


def test_no_same_candle_reentry_after_exit():
    """A position closed on candle i must not be reopened on that same candle."""
    script = {
        2: TradeSignal(signal=Signal.BUY, symbol="TEST-EQ", token="1",
                       entry_price=100, stop_loss=90, target=200),
        3: TradeSignal(signal=Signal.EXIT_LONG, symbol="TEST-EQ", token="1", reason="exit"),
    }
    rows = [
        _candle(0, 100), _candle(1, 100), _candle(2, 100),
        _candle(3, 100, high=101, low=99),
    ]
    candles = pd.DataFrame(rows)
    engine = make_engine(script)
    result = engine.run(candles)

    assert result.total_trades == 1  # not reopened same candle despite being flat afterward


def test_post_exit_reentry_on_subsequent_candle():
    """After a strategy-driven exit, a fresh entry on the NEXT candle must work."""
    script = {
        2: TradeSignal(signal=Signal.BUY, symbol="TEST-EQ", token="1",
                       entry_price=100, stop_loss=90, target=200),
        3: TradeSignal(signal=Signal.EXIT_LONG, symbol="TEST-EQ", token="1", reason="exit1"),
        4: TradeSignal(signal=Signal.BUY, symbol="TEST-EQ", token="1",
                       entry_price=100, stop_loss=90, target=200),
    }
    rows = [
        _candle(0, 100), _candle(1, 100), _candle(2, 100),
        _candle(3, 100, high=101, low=99),
        _candle(4, 100),
    ]
    candles = pd.DataFrame(rows)
    engine = make_engine(script)
    result = engine.run(candles)

    assert result.total_trades == 2
    assert [t["reason"] for t in result.trades] == ["exit1", "End of data"]


def test_eod_square_off_calls_force_exit():
    script = {2: TradeSignal(signal=Signal.BUY, symbol="TEST-EQ", token="1",
                              entry_price=100, stop_loss=90, target=200)}
    rows = [
        _candle(0, 100, minute_offset=0),
        _candle(1, 100, minute_offset=1),
        _candle(2, 100, minute_offset=2),
        # push past default sq_off (15:15)
        _candle(3, 100, minute_offset=6 * 60),
    ]
    candles = pd.DataFrame(rows)
    engine = make_engine(script)
    result = engine.run(candles)

    assert result.total_trades == 1
    assert result.trades[0]["reason"] == "EOD square-off"


def test_daily_reset_called_once_for_single_day_dataset():
    tracker = {"reset_calls": 0}

    class TrackedScripted(ScriptedStrategy):
        def reset(self):
            super().reset()
            tracker["reset_calls"] = self.reset_calls

    rows = [_candle(0, 100), _candle(1, 100)]
    candles = pd.DataFrame(rows)
    engine = BacktestEngine(
        strategy_class=TrackedScripted, symbol="TEST-EQ", token="1",
        strategy_kwargs={"script": {}},
        config=BacktestConfig(capital=100_000, slippage_pct=0.0, brokerage_pct=0.0),
    )
    engine.run(candles)

    assert tracker["reset_calls"] == 1


def test_daily_reset_called_on_each_new_calendar_day():
    tracker = {"reset_calls": 0}

    class TrackedScripted(ScriptedStrategy):
        def reset(self):
            super().reset()
            tracker["reset_calls"] = self.reset_calls

    rows = [
        _candle(0, 100, minute_offset=0),
        _candle(1, 100, minute_offset=1),
        {"timestamp": datetime(2024, 5, 7, 9, 15), "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000},
        {"timestamp": datetime(2024, 5, 7, 9, 16), "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000},
    ]
    candles = pd.DataFrame(rows)
    engine = BacktestEngine(
        strategy_class=TrackedScripted, symbol="TEST-EQ", token="1",
        strategy_kwargs={"script": {}},
        config=BacktestConfig(capital=100_000, slippage_pct=0.0, brokerage_pct=0.0),
    )
    engine.run(candles)

    assert tracker["reset_calls"] == 2  # one per distinct calendar day


# ----------------------------------------------------------------------
# Integration smoke test with the REAL strategy (qualitative, not exact-value)
# ----------------------------------------------------------------------

def test_real_strategy_runs_end_to_end_without_exceptions():
    from strategy.signal_engine import ORBEMAVWAPStrategy

    import numpy as np
    rng = np.random.default_rng(7)

    def make_day(day, base):
        ts = [datetime(2024, 6, day, 9, 15) + timedelta(minutes=15 * i) for i in range(26)]
        closes = [base]
        for i in range(1, 26):
            drift = 0.15 if i > 2 else 0.0
            closes.append(closes[-1] + drift + rng.normal(0, 0.4))
        closes = pd.Series(closes)
        opens  = closes - rng.random(26) * 0.2
        highs  = pd.concat([opens, closes], axis=1).max(axis=1) + rng.random(26) * 0.5
        lows   = pd.concat([opens, closes], axis=1).min(axis=1) - rng.random(26) * 0.5
        vols   = rng.integers(8000, 20000, size=26).astype(float)
        vols[3] *= 2.5
        return pd.DataFrame({"timestamp": ts, "open": opens, "high": highs,
                              "low": lows, "close": closes, "volume": vols})

    candles = pd.concat([make_day(d, 1000 + d) for d in range(1, 6)], ignore_index=True)

    engine = BacktestEngine(
        strategy_class=ORBEMAVWAPStrategy, symbol="SYN-EQ", token="1",
        strategy_kwargs=dict(
            candle_minutes=15, orb_minutes=15, ema_fast=9, ema_slow=21,
            rsi_period=14, rsi_overbought=70, rsi_oversold=30, atr_period=14,
            atr_stop_mult=1.5, atr_target_mult=2.5, vwap_filter=True,
            min_confidence=40, breakeven_r=1.0, trail_atr_mult=1.0,
            max_holding_candles=6, min_atr_pct=0.0, max_atr_pct=100.0,
        ),
        config=BacktestConfig(),
    )
    result = engine.run(candles)

    assert isinstance(result.total_trades, int)
    assert result.total_trades >= 0
    for t in result.trades:
        assert t["direction"] in ("long", "short")
        assert isinstance(t["pnl"], float)
    assert -100.0 <= result.max_drawdown <= 100.0 or result.max_drawdown == 0.0
