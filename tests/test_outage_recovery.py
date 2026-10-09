"""A provider blip must not lock users out; hostile tool numbers must not raise."""

import importlib
import time

import pytest

from wsp_core.attempt_engine_v3 import AttemptContext, AttemptEngine
from wsp_core.contract_v3 import (
    AttemptOutcome,
    Capability,
    CircuitState,
    ErrorClass,
    SkipReason,
)
from wsp_core.http_client import ProviderRequestError
from wsp_core.state_store_v3 import CircuitKey, SQLiteStateStore


def _key():
    return CircuitKey("brave", Capability.SEARCH, "provider://brave/search", "fp")


@pytest.mark.parametrize("error_class", [ErrorClass.TRANSIENT, ErrorClass.TIMEOUT])
def test_single_blip_does_not_block_the_next_request(tmp_path, error_class):
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    store.record_failure(_key(), error_class, now=100)
    assert store.admit(_key(), now=101).allowed is True
    store.record_failure(_key(), error_class, now=102)
    assert store.admit(_key(), now=103).allowed is True


@pytest.mark.parametrize("error_class", [ErrorClass.TRANSIENT, ErrorClass.TIMEOUT])
def test_three_consecutive_failures_open_the_circuit(tmp_path, error_class):
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    for now in (100, 101, 102):
        store.record_failure(_key(), error_class, now=now)
    decision = store.admit(_key(), now=103)
    assert decision.allowed is False
    assert decision.skip_reason is SkipReason.CIRCUIT_OPEN


def test_success_resets_the_consecutive_count(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    store.record_failure(_key(), ErrorClass.TRANSIENT, now=100)
    store.record_failure(_key(), ErrorClass.TRANSIENT, now=101)
    store.record_success(_key(), ErrorClass.TRANSIENT, now=102)
    store.record_failure(_key(), ErrorClass.TRANSIENT, now=103)
    assert store.admit(_key(), now=104).allowed is True


def _attempt(index):
    return AttemptContext(
        provider="brave",
        capability=Capability.SEARCH,
        endpoint="provider://brave/search",
        credential_fingerprint="fp",
        budget_scope=f"request-{index}",
        budget_window="request",
    )


def _ok():
    return {"results": [{"title": "t", "url": "https://example.com", "snippet": "s"}]}


def _http_503():
    raise ProviderRequestError("upstream", status_code=503, transient=True)


def _timeout():
    raise TimeoutError("provider call timed out")


def _run(engine, sequence):
    return [
        engine.execute(_attempt(index), operation, now=lambda: 1000)
        for index, operation in enumerate(sequence)
    ]


@pytest.mark.parametrize("failure", [_http_503, _timeout])
def test_failures_separated_by_successes_never_open_the_circuit(tmp_path, failure):
    # Each execute() sees only its own outcome: the success after a failure
    # has no error in its own call, yet it must still reset the count.
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    engine = AttemptEngine(store, max_attempts=1)

    executions = _run(engine, [failure, _ok] * 4)

    assert [e.receipt.outcome for e in executions] == [
        AttemptOutcome.FAILED,
        AttemptOutcome.SUCCESS,
    ] * 4
    assert all(e.receipt.skip_reason is None for e in executions)
    for error_class in (ErrorClass.TRANSIENT, ErrorClass.TIMEOUT):
        assert store.get_circuit(_attempt(0).circuit_key, error_class).failure_count == 0


@pytest.mark.parametrize("failure", [_http_503, _timeout])
def test_three_consecutive_failures_still_open_the_circuit_through_the_engine(
    tmp_path, failure
):
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    engine = AttemptEngine(store, max_attempts=1)

    executions = _run(engine, [failure, failure, failure, _ok])

    assert [e.receipt.outcome for e in executions[:3]] == [AttemptOutcome.FAILED] * 3
    assert executions[3].receipt.outcome is AttemptOutcome.SKIPPED
    assert executions[3].receipt.skip_reason is SkipReason.CIRCUIT_OPEN


def test_a_success_clears_blips_recorded_under_the_other_error_class(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    engine = AttemptEngine(store, max_attempts=1)

    _run(engine, [_http_503, _timeout, _ok, _http_503, _timeout, _ok])

    key = _attempt(0).circuit_key
    assert store.get_circuit(key, ErrorClass.TRANSIENT).failure_count == 0
    assert store.get_circuit(key, ErrorClass.TIMEOUT).failure_count == 0


def test_healthy_requests_do_not_touch_the_circuit_table_to_record_success(tmp_path):
    class CountingStore(SQLiteStateStore):
        successes = 0

        def record_success(self, key, error_class, *, now):
            type(self).successes += 1
            super().record_success(key, error_class, now=now)

    store = CountingStore(tmp_path / "state.sqlite3")
    engine = AttemptEngine(store, max_attempts=1)

    _run(engine, [_ok, _ok, _ok])
    assert CountingStore.successes == 0

    _run(engine, [_http_503, _ok, _ok])
    assert CountingStore.successes == 1


def test_admission_reports_the_buckets_a_success_must_clear(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    assert store.admit(_key(), now=100).failing_error_classes == ()
    store.record_failure(_key(), ErrorClass.TIMEOUT, now=100)
    decision = store.admit(_key(), now=101)
    assert decision.allowed is True
    assert decision.failing_error_classes == (ErrorClass.TIMEOUT,)


@pytest.mark.parametrize(
    ("error_class", "reason"),
    [
        (ErrorClass.AUTH, SkipReason.AUTH_BLOCKED),
        (ErrorClass.QUOTA, SkipReason.QUOTA_BLOCKED),
        (ErrorClass.RATE_LIMIT, SkipReason.RATE_LIMITED),
    ],
)
def test_auth_quota_and_rate_limit_still_block_on_first_failure(tmp_path, error_class, reason):
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    store.record_failure(_key(), error_class, now=100, retry_after_seconds=30)
    decision = store.admit(_key(), now=101)
    assert decision.allowed is False
    assert decision.skip_reason is reason


def test_last_resort_probe_answers_when_every_candidate_is_circuit_open(tmp_path, monkeypatch):
    search = importlib.import_module("wsp_core.search")
    from wsp_core.attempt_engine_v3 import AttemptContext, AttemptEngine

    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    engine = AttemptEngine(store, max_attempts=1)
    contexts = {
        name: AttemptContext(
            provider=name,
            capability=Capability.SEARCH,
            endpoint=f"provider://{name}/search",
            credential_fingerprint="fp",
            budget_scope="req",
            budget_window="request",
        )
        for name in ("brave", "serper")
    }
    for ctx in contexts.values():
        for _ in range(3):
            store.record_failure(ctx.circuit_key, ErrorClass.TRANSIENT, now=int(time.time()), retry_after_seconds=3600)

    calls = []

    def operation_for(name):
        def op():
            calls.append(name)
            return {"results": [{"title": "t", "url": "https://example.com", "snippet": "s"}]}
        return op

    race = search._race_providers(engine, ["brave", "serper"], contexts, operation_for, lambda _p: 5.0, None)
    assert race.provider is None
    assert all(r.skip_reason is SkipReason.CIRCUIT_OPEN for r in race.receipts)

    race = search._last_resort_probe(engine, store, "brave", contexts, operation_for, race)
    assert race.provider == "brave"
    assert calls == ["brave"]
    assert race.receipts[0].decision == "attempted"
    assert race.receipts[1].skip_reason is SkipReason.CIRCUIT_OPEN
    assert store.admit(contexts["brave"].circuit_key, now=int(time.time())).circuit_state is CircuitState.CLOSED
