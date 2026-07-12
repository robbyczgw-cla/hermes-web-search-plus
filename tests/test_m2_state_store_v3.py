from __future__ import annotations

import sqlite3

from contract_v3 import Capability, CircuitState, ErrorClass, SkipReason
from errors_v3 import classify_provider_error
from http_client import ProviderRequestError
from state_store_v3 import CircuitKey, SQLiteStateStore, credential_fingerprint


def _key(secret: str = "credential-a") -> CircuitKey:
    return CircuitKey(
        provider="serper",
        capability=Capability.SEARCH,
        endpoint="https://google.serper.dev/search",
        credential_fingerprint=credential_fingerprint(secret),
    )


def test_error_classifier_keeps_auth_quota_rate_and_transient_distinct():
    cases = [
        (ProviderRequestError("unauthorized", status_code=401), ErrorClass.AUTH),
        (ProviderRequestError("payment required", status_code=402), ErrorClass.QUOTA),
        (
            ProviderRequestError("rate limited", status_code=429, transient=True, retry_after=7),
            ErrorClass.RATE_LIMIT,
        ),
        (ProviderRequestError("upstream", status_code=503, transient=True), ErrorClass.TRANSIENT),
        (TimeoutError("timed out"), ErrorClass.TIMEOUT),
    ]

    for error, expected in cases:
        classified = classify_provider_error(error, provider="serper")
        assert classified.error_class is expected
        assert classified.provider == "serper"


def test_error_classifier_does_not_leak_exception_secret():
    error = ProviderRequestError("request failed with key super-secret-value", status_code=401)

    classified = classify_provider_error(error, provider="serper")

    assert "super-secret-value" not in classified.message
    assert classified.message == "Provider authentication failed"


def test_credential_fingerprint_is_stable_and_never_contains_secret():
    first = credential_fingerprint("super-secret-value")
    second = credential_fingerprint("super-secret-value")

    assert first == second
    assert "super-secret-value" not in first
    assert len(first) == 24


def test_state_store_admission_is_fail_closed_when_database_is_corrupt(tmp_path):
    path = tmp_path / "state.sqlite3"
    path.write_bytes(b"not sqlite")
    store = SQLiteStateStore(path)

    decision = store.admit(_key(), now=100)

    assert decision.allowed is False
    assert decision.circuit_state is CircuitState.UNKNOWN
    assert decision.skip_reason is SkipReason.CIRCUIT_OPEN
    assert decision.store_available is False


def test_circuit_rows_are_isolated_by_error_class(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    key = _key()

    store.record_failure(key, ErrorClass.AUTH, now=100)
    store.record_failure(key, ErrorClass.RATE_LIMIT, now=101, retry_after_seconds=30)

    auth = store.get_circuit(key, ErrorClass.AUTH)
    rate = store.get_circuit(key, ErrorClass.RATE_LIMIT)
    transient = store.get_circuit(key, ErrorClass.TRANSIENT)

    assert auth.state is CircuitState.BLOCKED_AUTH
    assert rate.state is CircuitState.OPEN
    assert rate.open_until == 131
    assert transient.state is CircuitState.CLOSED
    assert transient.failure_count == 0


def test_circuit_key_isolated_by_capability_endpoint_and_credential(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    blocked = _key("credential-a")
    store.record_failure(blocked, ErrorClass.AUTH, now=100)

    variants = [
        CircuitKey("serper", Capability.EXTRACT, blocked.endpoint, blocked.credential_fingerprint),
        CircuitKey("serper", Capability.SEARCH, "https://scrape.serper.dev", blocked.credential_fingerprint),
        _key("credential-b"),
    ]

    assert store.admit(blocked, now=101).allowed is False
    assert all(store.admit(key, now=101).allowed for key in variants)


def test_admission_maps_independent_open_buckets_to_specific_skip_reasons(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    key = _key()

    store.record_failure(key, ErrorClass.RATE_LIMIT, now=100, retry_after_seconds=20)
    decision = store.admit(key, now=101)
    assert decision.allowed is False
    assert decision.skip_reason is SkipReason.RATE_LIMITED

    store.record_success(key, ErrorClass.RATE_LIMIT, now=102)
    store.record_failure(key, ErrorClass.QUOTA, now=103, retry_after_seconds=40)
    decision = store.admit(key, now=104)
    assert decision.allowed is False
    assert decision.skip_reason is SkipReason.QUOTA_BLOCKED


def test_budget_ledger_reserve_commit_release_uses_abstract_units(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    store.configure_budget("request-1", "2026-07-12", limit_units=5)

    assert store.reserve_budget("request-1", "2026-07-12", units=3) is True
    assert store.reserve_budget("request-1", "2026-07-12", units=3) is False
    store.release_budget("request-1", "2026-07-12", units=1)
    assert store.reserve_budget("request-1", "2026-07-12", units=3) is True
    store.commit_budget("request-1", "2026-07-12", units=5)

    ledger = store.get_budget("request-1", "2026-07-12")
    assert ledger.limit_units == 5
    assert ledger.used_units == 5
    assert ledger.reserved_units == 0


def test_schema_initialization_is_idempotent_and_uses_wal(tmp_path):
    path = tmp_path / "state.sqlite3"
    SQLiteStateStore(path)
    SQLiteStateStore(path)

    connection = sqlite3.connect(path)
    try:
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    finally:
        connection.close()

    assert mode.lower() == "wal"
    assert version == 1
