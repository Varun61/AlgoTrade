import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from strategy.strategy_base import StrategyBase, TradeSignal, Signal


def test_rr_ratio_auto_computed_when_not_provided():
    sig = TradeSignal(signal=Signal.BUY, entry_price=100, stop_loss=95, target=115)
    assert sig.rr_ratio == pytest.approx(3.0)  # risk=5, reward=15


def test_rr_ratio_not_overwritten_if_explicitly_provided():
    sig = TradeSignal(signal=Signal.BUY, entry_price=100, stop_loss=95, target=115, rr_ratio=1.23)
    assert sig.rr_ratio == 1.23


def test_rr_ratio_zero_when_missing_fields():
    sig = TradeSignal(signal=Signal.HOLD)
    assert sig.rr_ratio == 0.0


def test_rr_ratio_zero_risk_does_not_raise():
    sig = TradeSignal(signal=Signal.BUY, entry_price=100, stop_loss=100, target=115)
    assert sig.rr_ratio == 0.0  # risk == 0 guarded, no ZeroDivisionError


@pytest.mark.parametrize("score,expected", [
    (100, "🔥 VERY HIGH"), (85, "🔥 VERY HIGH"),
    (84.9, "✅ HIGH"), (70, "✅ HIGH"),
    (69.9, "⚠️ MODERATE"), (55, "⚠️ MODERATE"),
    (54.9, "❌ LOW"), (0, "❌ LOW"),
])
def test_confidence_label_boundaries(score, expected):
    sig = TradeSignal(signal=Signal.BUY, confidence=score)
    assert sig.confidence_label() == expected


def test_is_entry_and_is_exit_are_mutually_exclusive():
    for sig_type in Signal:
        sig = TradeSignal(signal=sig_type)
        assert not (sig.is_entry() and sig.is_exit())
    assert TradeSignal(signal=Signal.BUY).is_entry()
    assert TradeSignal(signal=Signal.SELL).is_entry()
    assert TradeSignal(signal=Signal.EXIT_LONG).is_exit()
    assert TradeSignal(signal=Signal.EXIT_SHORT).is_exit()
    assert not TradeSignal(signal=Signal.HOLD).is_entry()
    assert not TradeSignal(signal=Signal.HOLD).is_exit()


def test_strategy_base_force_exit_default_is_noop():
    class Dummy(StrategyBase):
        def on_candle_close(self, candle, history):
            return TradeSignal(signal=Signal.HOLD, symbol=self.symbol, token=self.token)

    d = Dummy("X-EQ", "1")
    d.force_exit()  # must not raise
    d.reset()       # must not raise
