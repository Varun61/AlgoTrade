import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datetime import datetime

import pandas as pd
import pytest

from data.historical_fetcher import HistoricalFetcher, _is_rate_limited, _is_rate_limited_text


CANDLE_ROW = ["2026-09-22T09:15:00+05:30", 100.0, 101.0, 99.0, 100.5, 1000]


def _success_resp(n=1):
    return {"status": True, "data": [CANDLE_ROW for _ in range(n)]}


class FakeSmartConn:
    """Queues a sequence of getCandleData() responses/exceptions to return in order."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def getCandleData(self, params):
        self.calls += 1
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    # Keep tests fast; still lets us assert sleep was invoked via call count.
    monkeypatch.setattr(time, "sleep", lambda *_args, **_kwargs: None)


# ----------------------------------------------------------------------
# _is_rate_limited / _is_rate_limited_text
# ----------------------------------------------------------------------

def test_is_rate_limited_detects_message():
    resp = {"status": False, "message": "Access denied because of exceeding access rate", "data": None}
    assert _is_rate_limited(resp) is True


def test_is_rate_limited_detects_errorcode():
    resp = {"status": False, "message": "some other text", "errorcode": "AB1004", "data": None}
    assert _is_rate_limited(resp) is True


def test_is_rate_limited_false_for_unrelated_error():
    resp = {"status": False, "message": "Invalid Token", "errorcode": "AG8001", "data": None}
    assert _is_rate_limited(resp) is False


def test_is_rate_limited_false_for_empty_resp():
    assert _is_rate_limited(None) is False
    assert _is_rate_limited({}) is False


def test_is_rate_limited_text_matches_raw_exception_message():
    exc_text = "Couldn't parse the JSON response received from the server: b'Access denied because of exceeding access rate'"
    assert _is_rate_limited_text(exc_text) is True


def test_is_rate_limited_text_false_for_unrelated_exception():
    assert _is_rate_limited_text("Connection timed out") is False


# ----------------------------------------------------------------------
# HistoricalFetcher.fetch — success / retry / exhaustion behavior
# ----------------------------------------------------------------------

def _make_fetcher(responses):
    fetcher = HistoricalFetcher(FakeSmartConn(responses))
    fetcher._delay = 0.01  # keep test fast, real sleeping is patched out anyway
    return fetcher


def test_fetch_success_first_attempt_returns_df():
    fetcher = _make_fetcher([_success_resp(n=3)])
    df = fetcher.fetch("NSE", "123", 15, datetime(2026, 9, 1), datetime(2026, 9, 22))
    assert len(df) == 3
    assert list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume"]


def test_fetch_recovers_after_structured_rate_limit_response():
    responses = [
        {"status": False, "message": "Access denied because of exceeding access rate", "data": None},
        _success_resp(n=2),
    ]
    fetcher = _make_fetcher(responses)
    df = fetcher.fetch("NSE", "123", 15, datetime(2026, 9, 1), datetime(2026, 9, 22))
    assert len(df) == 2
    assert fetcher.obj.calls == 2


def test_fetch_recovers_after_rate_limit_exception():
    responses = [
        Exception("Couldn't parse the JSON response received from the server: b'Access denied because of exceeding access rate'"),
        _success_resp(n=1),
    ]
    fetcher = _make_fetcher(responses)
    df = fetcher.fetch("NSE", "123", 15, datetime(2026, 9, 1), datetime(2026, 9, 22))
    assert len(df) == 1
    assert fetcher.obj.calls == 2


def test_fetch_returns_empty_df_after_exhausting_retries():
    rate_limit_exc = Exception("Access denied because of exceeding access rate")
    fetcher = _make_fetcher([rate_limit_exc] * 5)  # retries default = 5
    df = fetcher.fetch("NSE", "123", 15, datetime(2026, 9, 1), datetime(2026, 9, 22))
    assert df.empty
    assert list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert fetcher.obj.calls == 5


def test_fetch_default_retries_is_five():
    import inspect
    sig = inspect.signature(HistoricalFetcher.fetch)
    assert sig.parameters["retries"].default == 5


def test_fetch_non_rate_limit_exception_still_retries_and_recovers():
    responses = [Exception("network blip"), _success_resp(n=1)]
    fetcher = _make_fetcher(responses)
    df = fetcher.fetch("NSE", "123", 15, datetime(2026, 9, 1), datetime(2026, 9, 22))
    assert len(df) == 1


def test_fetch_empty_data_treated_as_bad_response_and_retries():
    responses = [{"status": True, "data": []}, _success_resp(n=1)]
    fetcher = _make_fetcher(responses)
    df = fetcher.fetch("NSE", "123", 15, datetime(2026, 9, 1), datetime(2026, 9, 22))
    assert len(df) == 1
