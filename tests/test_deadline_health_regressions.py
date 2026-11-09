"""Caller deadlines must not become provider-health evidence (review B2)."""
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sqlite3
import threading
import time
from unittest import mock

import pytest

from wsp_core import config, extract, http_client, providers, search
from wsp_core.attempt_engine_v3 import AttemptContext, AttemptEngine
from wsp_core.contract_v3 import AttemptOutcome, Capability, ErrorClass, SkipReason
from wsp_core.errors_v3 import classify_provider_error
from wsp_core.http_client import ProviderRequestError, RequestDeadlineExceeded, request_deadline_scope
from wsp_core.state_store_v3 import SQLiteStateStore


def context(**changes):
    return replace(AttemptContext(
        provider="serper", capability=Capability.SEARCH,
        endpoint="https://example.test/search", credential_fingerprint="test-fingerprint",
        budget_scope="deadline-test", budget_window="request", budget_limit_units=10,
    ), **changes)


def assert_unused_budget(store, ctx):
    for scope, window in ((ctx.budget_scope, ctx.budget_window),
                          (ctx.daily_budget_scope, ctx.daily_budget_window)):
        if scope is not None:
            budget = store.get_budget(scope, window)
            assert (budget.used_units, budget.reserved_units) == (0, 0)


def test_healthy_local_server_deadlines_do_not_open_circuit(tmp_path, monkeypatch):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            time.sleep(1.0)
            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"results": []}')
            except (BrokenPipeError, ConnectionResetError):
                pass  # The short-deadline caller has already gone away.

        def log_message(self, format, *args):
            pass

    monkeypatch.setattr(http_client, "_proxy_applies", lambda *_: False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    engine = AttemptEngine(store)
    url = f"http://127.0.0.1:{server.server_port}/healthy"
    try:
        receipts = [engine.execute(
            context(deadline_monotonic=time.monotonic() + 0.5),
            lambda: http_client.make_get_request(url, {}, timeout=10),
        ).receipt for _ in range(3)]
        # Default-off daily accounting stays off; no provider-health write at all.
        with sqlite3.connect(store.path) as connection:
            assert connection.execute("SELECT count(*) FROM circuit_state").fetchone()[0] == 0
            assert connection.execute("SELECT count(*) FROM budget_ledger").fetchone()[0] == 0
        assert all(receipt.outcome is AttemptOutcome.CANCELLED for receipt in receipts)
        assert all(len(receipt.tries) == 1 for receipt in receipts)
        assert store.get_budget("deadline-test", "request").used_units == 3
        healthy = engine.execute(context(), lambda: http_client.make_get_request(url, {}, timeout=10))
        assert healthy.receipt.outcome is AttemptOutcome.SUCCESS
        assert len(requests) == 4
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_predispatch_deadline_releases_request_and_daily_budget_and_lease(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    ctx = context(daily_budget_scope="daily", daily_budget_window="2026-10-14",
                  daily_budget_limit_units=10)
    store.record_failure(ctx.circuit_key, ErrorClass.RATE_LIMIT, now=100, retry_after_seconds=1)
    engine = AttemptEngine(store)
    with mock.patch.object(http_client, "_pooled_open") as transport:
        def no_send():
            with request_deadline_scope(time.monotonic() - 1):
                return http_client.make_get_request("https://example.test", {})
        result = engine.execute(ctx, no_send, now=lambda: 102)
    transport.assert_not_called()
    assert result.receipt.outcome is AttemptOutcome.SKIPPED
    assert result.receipt.skip_reason is SkipReason.DEADLINE_EXCEEDED
    assert result.receipt.tries == []
    assert_unused_budget(store, ctx)
    assert store.get_circuit(ctx.circuit_key, ErrorClass.TIMEOUT).failure_count == 0
    assert store.admit(ctx.circuit_key, now=103).allowed


@pytest.mark.parametrize("error", [TimeoutError(), ProviderRequestError("timeout", transient=True)])
def test_dispatched_deadline_commits_budgets_but_releases_probe(tmp_path, monkeypatch, error):
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    ctx = context(deadline_monotonic=101, daily_budget_scope="daily",
                  daily_budget_window="2026-10-14", daily_budget_limit_units=10)
    store.record_failure(ctx.circuit_key, ErrorClass.RATE_LIMIT, now=100, retry_after_seconds=1)
    calls = []
    def operation():
        calls.append(1)
        clock[0] = 102.0
        raise error
    result = AttemptEngine(store).execute(ctx, operation, now=lambda: 102)
    assert result.receipt.outcome is AttemptOutcome.CANCELLED
    assert calls == [1]
    for scope, window in ((ctx.budget_scope, ctx.budget_window), ("daily", "2026-10-14")):
        budget = store.get_budget(scope, window)
        assert (budget.used_units, budget.reserved_units) == (1, 0)
    assert store.get_circuit(ctx.circuit_key, ErrorClass.TRANSIENT).failure_count == 0
    assert store.get_circuit(ctx.circuit_key, ErrorClass.TIMEOUT).failure_count == 0
    assert store.admit(ctx.circuit_key, now=103).allowed


@pytest.mark.parametrize("expired, status", [(False, None), (True, 503)])
def test_real_provider_failure_is_still_recorded(tmp_path, monkeypatch, expired, status):
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    ctx = context(deadline_monotonic=101)
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    def operation():
        clock[0] = 102 if expired else 100
        raise ProviderRequestError("upstream", transient=True, status_code=status)
    result = AttemptEngine(store, max_attempts=1).execute(ctx, operation)
    assert result.receipt.outcome is AttemptOutcome.FAILED
    assert store.get_circuit(ctx.circuit_key, ErrorClass.TRANSIENT).failure_count == 1


@pytest.mark.parametrize("loop_head", [False, True])
def test_retry_needs_one_second_of_useful_time(tmp_path, monkeypatch, loop_head):
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    calls = []
    sleeps = []
    def operation():
        calls.append(1)
        raise ProviderRequestError("upstream", status_code=429, retry_after=0)
    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] = 101.999
    ctx = context(deadline_monotonic=102 if loop_head else 100.001)
    result = AttemptEngine(store, sleep=sleep).execute(ctx, operation)
    assert calls == [1]
    assert sleeps == ([0.0] if loop_head else [])
    assert result.receipt.outcome is AttemptOutcome.FAILED
    assert result.receipt.retry_count == 0
    assert len(result.receipt.tries) == 1
    assert store.get_circuit(ctx.circuit_key, ErrorClass.RATE_LIMIT).failure_count == 1


def test_predispatch_deadline_on_retry_keeps_only_the_paid_try(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    calls = []
    def operation():
        calls.append(1)
        if len(calls) == 1:
            raise ProviderRequestError("upstream", status_code=503)
        raise RequestDeadlineExceeded()
    result = AttemptEngine(store, sleep=lambda _: None).execute(context(), operation)
    assert calls == [1, 1]
    assert result.receipt.outcome is AttemptOutcome.FAILED
    assert len(result.receipt.tries) == 1
    assert result.receipt.retry_count == 0
    assert store.get_budget("deadline-test", "request").used_units == 1
    assert store.get_circuit(context().circuit_key, ErrorClass.TRANSIENT).failure_count == 1
    assert store.get_circuit(context().circuit_key, ErrorClass.TIMEOUT).failure_count == 0


def test_brave_pacing_never_sleeps_past_deadline(monkeypatch):
    clock = [100.0]
    sleeps = []
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds
    monkeypatch.setattr(time, "sleep", sleep)
    pacer = providers._CallPacer()
    pacer._next_slot = 102.0
    with request_deadline_scope(100.25):
        with pytest.raises(RequestDeadlineExceeded):
            pacer.wait_turn(2)
    assert sleeps == [0.25]
    assert pacer._next_slot == 102.0


@pytest.mark.parametrize("provider", ["you", "searxng"])
def test_search_transports_preserve_predispatch_deadline(provider):
    fn = getattr(providers, f"search_{provider}")
    with request_deadline_scope(time.monotonic() - 1):
        with pytest.raises(RequestDeadlineExceeded):
            fn("query", "https://example.test" if provider == "searxng" else "test-key")


@pytest.mark.parametrize("error, should_record", [
    (RequestDeadlineExceeded(), False),
    (ProviderRequestError("timed out", transient=True), False),
    (ProviderRequestError("unavailable", status_code=503, transient=True), True),
])
def test_search_stats_exclude_caller_deadline_aborts(monkeypatch, error, should_record):
    cfg = config._deepcopy_default_config()
    args = search.default_search_args(cfg)
    args.provider, args.query, args.no_cache = "serper", "query", True
    args._v3_engine_owned_attempt = True
    monkeypatch.setattr(search, "validate_api_key", lambda *_: "test-key")
    with mock.patch.object(providers, "search_serper", side_effect=error), \
         mock.patch.object(search, "record_provider_outcome") as record, \
         request_deadline_scope(time.monotonic() - 1):
        with pytest.raises(ProviderRequestError):
            search._execute_search_request_core(args, cfg)
    assert record.call_count == int(should_record)


def test_all_not_tried_extract_batch_is_timeout_not_contract(monkeypatch):
    cfg = config._deepcopy_default_config()
    monkeypatch.setattr(extract, "get_api_key", lambda *_: "test-key")
    urls = ["https://example.test/a", "https://example.test/b"]
    with request_deadline_scope(time.monotonic() - 1):
        with pytest.raises(RequestDeadlineExceeded) as caught:
            extract._extract_plus_core(urls, provider="firecrawl", config=cfg)
    assert classify_provider_error(caught.value, provider="firecrawl").error_class is ErrorClass.TIMEOUT
