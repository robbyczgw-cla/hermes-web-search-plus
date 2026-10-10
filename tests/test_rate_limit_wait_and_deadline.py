"""Rate-limit waits are bounded and the tool's wall-clock deadline reaches the engine.

Before 5.0.1 a 429 with ``Retry-After: 3600`` slept an hour inside the request,
``inf`` raised OverflowError before the circuit was recorded, a missing header
meant an instant retry, and the tool timeout never reached the engine, so
billable calls kept starting after the user had already seen a timeout.
"""

from __future__ import annotations

import time
from email.message import Message
from urllib.error import HTTPError

import pytest

from plugin_loader import load_plugin
from wsp_core import http_client, provider_registry, providers, search
from wsp_core.attempt_engine_v3 import AttemptContext, AttemptEngine
from wsp_core.compat_v3 import legacy_request_to_v3
from wsp_core.config import _deepcopy_default_config
from wsp_core.contract_v3 import AttemptOutcome, Capability, ErrorClass, SkipReason
from wsp_core.http_client import ProviderRequestError
from wsp_core.provider_health import MAX_RETRY_AFTER_WAIT_SECONDS
from wsp_core.state_store_v3 import (
    MAX_OPEN_SECONDS,
    SQLiteStateStore,
    credential_fingerprint,
)

wsp = load_plugin("wsp_plugin_rate_limit_deadline_under_test")


def _context(**overrides) -> AttemptContext:
    return AttemptContext(
        provider="serper",
        capability=Capability.SEARCH,
        endpoint="https://google.serper.dev/search",
        credential_fingerprint=credential_fingerprint(
            "credential", local_secret=b"test-local-secret"
        ),
        budget_scope="request-1",
        budget_window="request",
        **overrides,
    )


def _engine(tmp_path, sleeps, max_attempts=3):
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    return store, AttemptEngine(store, max_attempts=max_attempts, sleep=sleeps.append)


def _rate_limited(retry_after):
    def operation():
        operation.calls += 1
        raise ProviderRequestError(
            "slow down", status_code=429, transient=True, retry_after=retry_after
        )

    operation.calls = 0
    return operation


# --- engine: bounded waits ---------------------------------------------------


def test_retry_after_above_the_cap_is_not_waited_out(tmp_path):
    sleeps: list[float] = []
    store, engine = _engine(tmp_path, sleeps)
    operation = _rate_limited(3600)

    execution = engine.execute(_context(), operation, now=lambda: 100)

    assert sleeps == []
    assert operation.calls == 1
    assert execution.receipt.outcome is AttemptOutcome.FAILED
    assert execution.receipt.error.error_class is ErrorClass.RATE_LIMIT
    record = store.get_circuit(_context().circuit_key, ErrorClass.RATE_LIMIT)
    assert record.open_until == 100 + 3600


def test_retry_after_within_the_cap_is_honoured(tmp_path):
    sleeps: list[float] = []
    _store, engine = _engine(tmp_path, sleeps)
    calls = []

    def operation():
        calls.append(1)
        if len(calls) == 1:
            raise ProviderRequestError("slow", status_code=429, transient=True, retry_after=7)
        return {"results": [{"url": "https://example.com"}]}

    execution = engine.execute(_context(), operation, now=lambda: 100)

    assert sleeps == [7.0]
    assert execution.receipt.outcome is AttemptOutcome.SUCCESS


def test_retry_after_exactly_at_the_cap_is_still_waited(tmp_path):
    sleeps: list[float] = []
    _store, engine = _engine(tmp_path, sleeps, max_attempts=2)
    operation = _rate_limited(MAX_RETRY_AFTER_WAIT_SECONDS)

    engine.execute(_context(), operation, now=lambda: 100)

    assert sleeps == [MAX_RETRY_AFTER_WAIT_SECONDS]
    assert operation.calls == 2


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan"), -5.0])
def test_non_finite_retry_after_does_not_crash_and_records_the_circuit(tmp_path, value):
    sleeps: list[float] = []
    store, engine = _engine(tmp_path, sleeps, max_attempts=2)
    operation = _rate_limited(value)

    execution = engine.execute(_context(), operation, now=lambda: 100)

    assert execution.receipt.outcome is AttemptOutcome.FAILED
    assert operation.calls == 2
    assert len(sleeps) == 1 and 0 < sleeps[0] <= MAX_RETRY_AFTER_WAIT_SECONDS
    assert all(
        item["error"]["retry_after_ms"] is None for item in execution.receipt.tries
    )
    record = store.get_circuit(_context().circuit_key, ErrorClass.RATE_LIMIT)
    assert record.open_until is not None and record.open_until <= 100 + MAX_OPEN_SECONDS


def test_missing_retry_after_backs_off_instead_of_retrying_instantly(tmp_path):
    sleeps: list[float] = []
    _store, engine = _engine(tmp_path, sleeps, max_attempts=3)
    calls = []

    def operation():
        calls.append(1)
        if len(calls) < 3:
            raise ProviderRequestError("down", status_code=503, transient=True)
        return {"results": []}

    engine.execute(_context(), operation, now=lambda: 100)

    assert len(sleeps) == 2
    assert 1.0 <= sleeps[0] <= 1.5
    assert 3.0 <= sleeps[1] <= 4.5


# --- engine: deadline --------------------------------------------------------


def test_no_retry_starts_when_the_wait_would_pass_the_deadline(tmp_path):
    sleeps: list[float] = []
    _store, engine = _engine(tmp_path, sleeps)
    operation = _rate_limited(20)
    context = _context(deadline_monotonic=time.monotonic() + 5)

    execution = engine.execute(context, operation, now=lambda: 100)

    assert sleeps == []
    assert operation.calls == 1
    assert execution.receipt.outcome is AttemptOutcome.FAILED


def test_backoff_retry_is_skipped_when_the_deadline_is_too_close(tmp_path):
    sleeps: list[float] = []
    _store, engine = _engine(tmp_path, sleeps)
    calls = []

    def operation():
        calls.append(1)
        raise ProviderRequestError("down", status_code=503, transient=True)

    engine.execute(
        _context(deadline_monotonic=time.monotonic() + 0.5), operation, now=lambda: 100
    )

    assert calls == [1]
    assert sleeps == []


def test_no_provider_call_starts_after_the_deadline(tmp_path):
    sleeps: list[float] = []
    _store, engine = _engine(tmp_path, sleeps)
    operation = _rate_limited(0)

    execution = engine.execute(
        _context(deadline_monotonic=time.monotonic() - 1), operation, now=lambda: 100
    )

    assert operation.calls == 0
    assert execution.receipt.outcome is AttemptOutcome.SKIPPED
    assert execution.receipt.skip_reason is SkipReason.DEADLINE_EXCEEDED


# --- circuit window cap ------------------------------------------------------


@pytest.mark.parametrize("seconds", [10**9, 86400, 3601])
def test_circuit_window_from_retry_after_is_capped(tmp_path, seconds):
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    key = _context().circuit_key

    record = store.record_failure(
        key, ErrorClass.RATE_LIMIT, now=1000, retry_after_seconds=float(seconds)
    )

    assert record.open_until == 1000 + MAX_OPEN_SECONDS == 1000 + 3600


def test_circuit_window_below_the_cap_is_kept_and_non_finite_uses_the_default(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    key = _context().circuit_key

    short = store.record_failure(key, ErrorClass.RATE_LIMIT, now=1000, retry_after_seconds=120.0)
    assert short.open_until == 1120
    infinite = store.record_failure(
        key, ErrorClass.RATE_LIMIT, now=1000, retry_after_seconds=float("inf")
    )
    assert infinite.open_until == 1060


# --- Retry-After parsing -----------------------------------------------------


@pytest.mark.parametrize("header", ["inf", "-inf", "1e999", "nan", "Infinity"])
def test_http_client_rejects_non_finite_retry_after(header):
    headers = Message()
    headers["Retry-After"] = header
    error = HTTPError("https://example.com/", 429, "rate", headers, None)
    assert http_client._parse_retry_after(error) is None


def test_http_client_still_parses_seconds_and_dates():
    headers = Message()
    headers["Retry-After"] = "12"
    assert http_client._parse_retry_after(HTTPError("u", 429, "r", headers, None)) == 12.0
    dated = Message()
    dated["Retry-After"] = "Wed, 21 Oct 2099 07:28:00 GMT"
    assert http_client._parse_retry_after(HTTPError("u", 429, "r", dated, None)) > 0


@pytest.mark.parametrize("header", ["inf", "1e999", "nan", "-1"])
def test_octen_rejects_non_finite_retry_after(header):
    spec = provider_registry.PROVIDER_SPECS["octen"]
    headers = Message()
    headers["Retry-After"] = header
    error = HTTPError("https://api.octen.ai/", 429, "rate", headers, None)
    assert spec.execute_search.__globals__["_retry_after"](error) is None


# --- deadline plumbing -------------------------------------------------------


def test_legacy_request_carries_a_positive_wall_time_only():
    with_deadline = legacy_request_to_v3(
        Capability.SEARCH, {"query": "q", "max_wall_time_ms": 72000}
    )
    assert with_deadline.budget == {"max_wall_time_ms": 72000}
    for bad in (None, 0, -1, True, "5"):
        assert legacy_request_to_v3(
            Capability.SEARCH, {"query": "q", "max_wall_time_ms": bad}
        ).budget == {}
    extract = legacy_request_to_v3(
        Capability.EXTRACT, {"urls": ["https://example.com"], "max_wall_time_ms": 5000}
    )
    assert extract.budget == {"max_wall_time_ms": 5000}


def test_wall_time_deadline_does_not_split_the_search_cache_key():
    from wsp_core.cache_v3 import derive_cache_key

    plain = legacy_request_to_v3(Capability.SEARCH, {"query": "q"})
    tool = legacy_request_to_v3(
        Capability.SEARCH, {"query": "q", "max_wall_time_ms": 72000}
    )
    research_tool = legacy_request_to_v3(
        Capability.SEARCH, {"query": "q", "max_wall_time_ms": 87000}
    )
    assert derive_cache_key(plain) == derive_cache_key(tool) == derive_cache_key(research_tool)


def test_tool_timeout_becomes_the_engine_deadline_for_search_and_extract(monkeypatch):
    seen = {}

    class FakeSearch:
        @staticmethod
        def run_search_request(**kwargs):
            seen["search"] = kwargs["max_wall_time_ms"]
            return {"results": []}

        @staticmethod
        def run_extract_request(urls, **kwargs):
            seen["extract"] = kwargs["max_wall_time_ms"]
            return {"results": []}

    monkeypatch.setattr(wsp, "_load_search_module", lambda: FakeSearch)

    wsp._run_search("q")
    wsp._run_search("q", mode="research", research_time_budget=120)
    wsp._run_extract(["https://example.com"])

    assert seen["extract"] == (90 - 3) * 1000
    # research: the widened timeout (budget + 15 s) minus the margin
    assert seen["search"] == (135 - 3) * 1000


# --- end to end through the search engine ------------------------------------


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setenv("SERPER_API_KEY", "serper-test-key-123456")


def _config():
    config = _deepcopy_default_config()
    config["v3"] = {"attempt_timeout_seconds": 5}
    return config


def test_explicit_provider_429_with_huge_retry_after_returns_promptly(monkeypatch, keyed):
    calls = []

    def serper(query, api_key, max_results=5, **_kwargs):
        calls.append(1)
        raise ProviderRequestError("limited", status_code=429, transient=True, retry_after=3600)

    monkeypatch.setattr(providers, "search_serper", serper)
    started = time.monotonic()
    result = search.run_search_request(
        query="rate limit", provider="serper", no_cache=True, config=_config()
    )

    assert time.monotonic() - started < 10
    assert result.get("error")
    assert calls == [1]


def test_no_provider_attempt_starts_after_the_request_deadline(monkeypatch, keyed):
    calls = []

    def serper(query, api_key, max_results=5, **_kwargs):
        calls.append(time.monotonic())
        time.sleep(0.3)
        raise ProviderRequestError("unavailable", status_code=503, transient=True)

    monkeypatch.setattr(providers, "search_serper", serper)
    result = search.run_search_request(
        query="deadline", provider="serper", no_cache=True, config=_config(),
        max_wall_time_ms=500,
    )

    assert result.get("error")
    assert len(calls) == 1
