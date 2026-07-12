from __future__ import annotations

from contract_v3 import (
    AttemptOutcome,
    Capability,
    CircuitState,
    ErrorClass,
    SkipReason,
)
from attempt_engine_v3 import AttemptContext, AttemptEngine
from http_client import ProviderRequestError
from state_store_v3 import SQLiteStateStore, credential_fingerprint


def _context(*, budget_units: int = 1, budget_limit_units: int = 3) -> AttemptContext:
    return AttemptContext(
        provider="serper",
        capability=Capability.SEARCH,
        endpoint="https://google.serper.dev/search",
        credential_fingerprint=credential_fingerprint(
            "credential", local_secret=b"test-local-secret"
        ),
        budget_scope="request-1",
        budget_window="request",
        budget_units=budget_units,
        budget_limit_units=budget_limit_units,
    )


def test_attempt_engine_retries_transient_and_records_one_provider_receipt(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    engine = AttemptEngine(store, max_attempts=3, sleep=lambda _seconds: None)
    calls = []

    def operation():
        calls.append("call")
        if len(calls) == 1:
            raise ProviderRequestError("upstream", status_code=503, transient=True)
        return {"results": [{"url": "https://example.com"}]}

    execution = engine.execute(_context(), operation, now=lambda: 100)

    assert len(calls) == 2
    assert execution.payload is not None
    assert execution.receipt.outcome is AttemptOutcome.SUCCESS
    assert execution.receipt.retry_count == 1
    assert execution.receipt.result_count == 1
    assert execution.receipt.circuit_state_before is CircuitState.CLOSED
    assert execution.receipt.circuit_state_after is CircuitState.CLOSED
    assert execution.receipt.endpoint_id == "serper:search"
    assert [item["outcome"] for item in execution.receipt.tries] == [
        "error",
        "success",
    ]
    assert execution.receipt.tries[0]["error"]["error_class"] == "transient"
    assert execution.receipt.tries[1]["error"] is None
    assert store.get_budget("request-1", "request").used_units == 2


def test_attempt_ids_are_unique_for_same_context_and_second(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    engine = AttemptEngine(store, max_attempts=1)

    first = engine.execute(_context(), lambda: {"results": []}, now=lambda: 100)
    second = engine.execute(_context(), lambda: {"results": []}, now=lambda: 100)

    assert first.receipt.attempt_id != second.receipt.attempt_id


def test_auth_failure_is_not_retried_and_blocks_same_credential(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    engine = AttemptEngine(store, max_attempts=3, sleep=lambda _seconds: None)
    calls = []

    def operation():
        calls.append("call")
        raise ProviderRequestError("bad key", status_code=401)

    failed = engine.execute(_context(), operation, now=lambda: 100)
    blocked = engine.execute(_context(), operation, now=lambda: 101)

    assert len(calls) == 1
    assert failed.receipt.outcome is AttemptOutcome.FAILED
    assert failed.receipt.error.error_class is ErrorClass.AUTH
    assert failed.receipt.retry_count == 0
    assert len(failed.receipt.tries) == 1
    assert failed.receipt.tries[0]["outcome"] == "error"
    assert failed.receipt.tries[0]["error"]["error_class"] == "auth"
    assert blocked.receipt.outcome is AttemptOutcome.SKIPPED
    assert blocked.receipt.skip_reason is SkipReason.AUTH_BLOCKED
    assert blocked.receipt.endpoint_id == "serper:search"
    assert blocked.receipt.decision == "skipped"
    assert blocked.receipt.tries == []


def test_unavailable_state_store_degrades_without_skipping_provider(tmp_path):
    path = tmp_path / "state.sqlite3"
    path.write_bytes(b"corrupt")
    engine = AttemptEngine(SQLiteStateStore(path))
    calls = []

    execution = engine.execute(
        _context(), lambda: calls.append("called") or {"results": []}, now=lambda: 100
    )

    assert calls == ["called"]
    assert execution.payload == {"results": []}
    assert execution.receipt.outcome is AttemptOutcome.SUCCESS
    assert execution.receipt.circuit_state_before is CircuitState.UNKNOWN
    assert execution.receipt.budget_decision == "store_unavailable"


def test_store_loss_after_reservation_never_discards_provider_outcome(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    engine = AttemptEngine(store, max_attempts=1)

    def success():
        store._available = False
        return {"results": [{"url": "https://example.com"}]}

    successful = engine.execute(_context(), success, now=lambda: 100)
    assert successful.payload is not None
    assert successful.receipt.outcome is AttemptOutcome.SUCCESS

    store = SQLiteStateStore(tmp_path / "state-2.sqlite3")
    engine = AttemptEngine(store, max_attempts=1)

    def failure():
        store._available = False
        raise ProviderRequestError("upstream", status_code=503, transient=True)

    failed = engine.execute(_context(), failure, now=lambda: 100)
    assert failed.payload is None
    assert failed.receipt.outcome is AttemptOutcome.FAILED
    assert failed.receipt.error is not None
    assert failed.receipt.error.error_class is ErrorClass.TRANSIENT


def test_store_loss_during_budget_reservation_does_not_skip_provider(
    tmp_path, monkeypatch
):
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    engine = AttemptEngine(store, max_attempts=1)
    calls = []

    def fail_reservation(*_args, **_kwargs):
        store._available = False
        return False

    monkeypatch.setattr(store, "reserve_budget", fail_reservation)
    execution = engine.execute(
        _context(),
        lambda: calls.append("called") or {"results": []},
        now=lambda: 100,
    )

    assert calls == ["called"]
    assert execution.receipt.outcome is AttemptOutcome.SUCCESS
    assert execution.receipt.budget_decision == "store_unavailable"


def test_store_loss_during_reconciliation_preserves_provider_success(
    tmp_path, monkeypatch
):
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    engine = AttemptEngine(store, max_attempts=1)

    def fail_commit(*_args, **_kwargs):
        store._available = False
        return False

    monkeypatch.setattr(store, "commit_budget", fail_commit)
    execution = engine.execute(
        _context(),
        lambda: {"results": [{"url": "https://example.com"}]},
        now=lambda: 100,
    )

    assert execution.payload is not None
    assert execution.receipt.outcome is AttemptOutcome.SUCCESS
    assert execution.receipt.budget_decision == "store_unavailable"


def test_budget_is_admitted_before_provider_call(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    engine = AttemptEngine(store)
    calls = []

    execution = engine.execute(
        _context(budget_limit_units=0),
        lambda: calls.append("called") or {"results": []},
        now=lambda: 100,
    )

    assert calls == []
    assert execution.receipt.outcome is AttemptOutcome.SKIPPED
    assert execution.receipt.skip_reason is SkipReason.BUDGET_BLOCKED
    assert execution.receipt.budget_decision == "blocked"


def test_successful_half_open_probe_clears_auth_bucket(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    context = _context()
    store.record_failure(context.circuit_key, ErrorClass.AUTH, now=100)
    engine = AttemptEngine(store, max_attempts=1)

    execution = engine.execute(
        context,
        lambda: {"results": []},
        now=lambda: 400,
    )

    assert execution.receipt.outcome is AttemptOutcome.SUCCESS
    assert execution.receipt.circuit_state_before is CircuitState.HALF_OPEN
    assert store.get_circuit(context.circuit_key, ErrorClass.AUTH).state is CircuitState.CLOSED


def test_admission_runs_before_each_retry(tmp_path):
    class CountingStore(SQLiteStateStore):
        def __init__(self, path):
            self.admissions = 0
            super().__init__(path)

        def admit(self, key, *, now):
            self.admissions += 1
            return super().admit(key, now=now)

    store = CountingStore(tmp_path / "state.sqlite3")
    engine = AttemptEngine(store, max_attempts=3, sleep=lambda _seconds: None)
    calls = []

    def operation():
        calls.append("call")
        if len(calls) < 3:
            raise ProviderRequestError("upstream", status_code=503, transient=True)
        return {"results": []}

    engine.execute(_context(), operation, now=lambda: 100)

    assert len(calls) == 3
    assert store.admissions == 3
