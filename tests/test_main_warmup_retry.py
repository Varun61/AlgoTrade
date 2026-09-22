import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
import pytest

import main


HIST_COLS = ["timestamp", "open", "high", "low", "close", "volume"]


def _empty_df():
    return pd.DataFrame(columns=HIST_COLS)


def _filled_df(n=5):
    return pd.DataFrame({c: range(n) for c in HIST_COLS})


class FakeFetcher:
    """Returns a queued warm-up result per token, tracking call order."""

    def __init__(self, results_by_token):
        self._results = results_by_token
        self.calls = []

    def fetch_warmup(self, exchange, token, interval_minutes, lookback_days=30):
        self.calls.append(token)
        return self._results[token]


WATCHLIST = [
    {"exchange": "NSE", "segment": 1, "token": "1", "symbol": "AAA-EQ"},
    {"exchange": "NSE", "segment": 1, "token": "2", "symbol": "BBB-EQ"},
    {"exchange": "NSE", "segment": 1, "token": "3", "symbol": "CCC-EQ"},
]


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)


def test_no_retry_needed_when_all_histories_present():
    histories = {"1": _filled_df(), "2": _filled_df(), "3": _filled_df()}
    fetcher = FakeFetcher({})  # should never be called
    still_failed = main.retry_failed_warmups(WATCHLIST, histories, fetcher, 15, 30)
    assert still_failed == []
    assert fetcher.calls == []


def test_retries_only_empty_symbols():
    histories = {"1": _filled_df(), "2": _empty_df(), "3": _empty_df()}
    fetcher = FakeFetcher({"2": _filled_df(), "3": _filled_df()})
    still_failed = main.retry_failed_warmups(WATCHLIST, histories, fetcher, 15, 30)
    assert still_failed == []
    assert sorted(fetcher.calls) == ["2", "3"]
    assert not histories["2"].empty
    assert not histories["3"].empty
    assert not histories["1"].empty  # untouched


def test_reports_symbols_still_empty_after_retry():
    histories = {"1": _filled_df(), "2": _empty_df(), "3": _empty_df()}
    fetcher = FakeFetcher({"2": _filled_df(), "3": _empty_df()})  # "3" still fails
    still_failed = main.retry_failed_warmups(WATCHLIST, histories, fetcher, 15, 30)
    assert still_failed == ["CCC-EQ"]


def test_sleeps_for_cooldown_before_retrying(monkeypatch):
    sleep_calls = []
    monkeypatch.setattr(time, "sleep", lambda secs: sleep_calls.append(secs))
    histories = {"1": _filled_df(), "2": _empty_df(), "3": _filled_df()}
    fetcher = FakeFetcher({"2": _filled_df()})
    main.retry_failed_warmups(WATCHLIST, histories, fetcher, 15, 30, cooldown_secs=7.0)
    assert sleep_calls == [7.0]


def test_does_not_sleep_when_nothing_failed(monkeypatch):
    sleep_calls = []
    monkeypatch.setattr(time, "sleep", lambda secs: sleep_calls.append(secs))
    histories = {"1": _filled_df(), "2": _filled_df(), "3": _filled_df()}
    fetcher = FakeFetcher({})
    main.retry_failed_warmups(WATCHLIST, histories, fetcher, 15, 30, cooldown_secs=7.0)
    assert sleep_calls == []
