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
        credential_fingerprint=credential_fingerprint("credential"),
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
    assert store.get_budget("request-1", "request").used_units == 2


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
    assert blocked.receipt.outcome is AttemptOutcome.SKIPPED
    assert blocked.receipt.skip_reason is SkipReason.AUTH_BLOCKED


def test_fail_closed_state_store_skips_before_provider_call(tmp_path):
    path = tmp_path / "state.sqlite3"
    path.write_bytes(b"corrupt")
    engine = AttemptEngine(SQLiteStateStore(path))
    calls = []

    execution = engine.execute(
        _context(), lambda: calls.append("called") or {"results": []}, now=lambda: 100
    )

    assert calls == []
    assert execution.receipt.outcome is AttemptOutcome.SKIPPED
    assert execution.receipt.skip_reason is SkipReason.CIRCUIT_OPEN
    assert execution.receipt.circuit_state_before is CircuitState.UNKNOWN


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
