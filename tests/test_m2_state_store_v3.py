from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import sqlite3

import wsp_core.state_store_v3 as state_store_module

from wsp_core.contract_v3 import Capability, CircuitState, ErrorClass, SkipReason
from wsp_core.errors_v3 import classify_provider_error
from wsp_core.http_client import ProviderRequestError
from wsp_core.state_store_v3 import SCHEMA_VERSION, CircuitKey, SQLiteStateStore, credential_fingerprint


def _key(secret: str = "credential-a") -> CircuitKey:
    return CircuitKey(
        provider="serper",
        capability=Capability.SEARCH,
        endpoint="https://google.serper.dev/search",
        credential_fingerprint=credential_fingerprint(
            secret, local_secret=b"test-local-secret"
        ),
    )


def test_error_classifier_keeps_auth_quota_rate_and_transient_distinct():
    cases = [
        (ProviderRequestError("unauthorized", status_code=401), ErrorClass.AUTH),
        (ProviderRequestError("payment required", status_code=402), ErrorClass.QUOTA),
        (ProviderRequestError("usage limit", status_code=432), ErrorClass.QUOTA),
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
    first = credential_fingerprint(
        "super-secret-value", local_secret=b"machine-a"
    )
    second = credential_fingerprint(
        "super-secret-value", local_secret=b"machine-a"
    )
    other_machine = credential_fingerprint(
        "super-secret-value", local_secret=b"machine-b"
    )

    assert first == second
    assert first != other_machine
    assert "super-secret-value" not in first
    assert len(first) == 24


def test_state_store_persists_private_hmac_secret_beside_database(tmp_path):
    path = tmp_path / "state.sqlite3"
    first = SQLiteStateStore(path)
    first_fingerprint = first.fingerprint_credential("credential")
    second = SQLiteStateStore(path)

    assert second.fingerprint_credential("credential") == first_fingerprint
    assert second.secret_path == tmp_path / "state.sqlite3.secret"
    assert second.secret_path.stat().st_mode & 0o777 == 0o600
    assert second.secret_path.read_bytes() != b"credential"


def test_state_store_admission_degrades_when_database_is_corrupt(tmp_path):
    path = tmp_path / "state.sqlite3"
    path.write_bytes(b"not sqlite")
    store = SQLiteStateStore(path)

    decision = store.admit(_key(), now=100)

    assert decision.allowed is True
    assert decision.circuit_state is CircuitState.UNKNOWN
    assert decision.skip_reason is None
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


def test_auth_circuit_allows_exactly_one_half_open_probe_after_cooldown(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    key = _key()

    record = store.record_failure(key, ErrorClass.AUTH, now=100)

    assert record.open_until == 400
    assert store.admit(key, now=399).allowed is False
    probe = store.admit(key, now=400)
    competing = store.admit(key, now=400)
    assert probe.allowed is True
    assert probe.circuit_state is CircuitState.HALF_OPEN
    assert probe.blocking_error_class is ErrorClass.AUTH
    assert competing.allowed is False
    assert competing.circuit_state is CircuitState.HALF_OPEN

    store.record_success(key, ErrorClass.AUTH, now=401)
    recovered = store.admit(key, now=401)
    assert recovered.allowed is True
    assert recovered.circuit_state is CircuitState.CLOSED


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


def test_budget_reservation_is_atomic_across_concurrent_workers(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    store.configure_budget("shared", "window", limit_units=5)

    with ThreadPoolExecutor(max_workers=20) as executor:
        admitted = list(
            executor.map(
                lambda _index: store.reserve_budget("shared", "window", units=1),
                range(20),
            )
        )

    ledger = store.get_budget("shared", "window")
    assert sum(admitted) == 5
    assert ledger.used_units == 0
    assert ledger.reserved_units == 5


def test_budget_reconciliation_commits_actual_and_releases_unused_units(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    store.configure_budget("request", "window", limit_units=5)
    assert store.reserve_budget("request", "window", units=3)

    store.reconcile_budget(
        "request", "window", reserved_units=3, actual_units=1
    )

    ledger = store.get_budget("request", "window")
    assert ledger.used_units == 1
    assert ledger.reserved_units == 0


def test_admission_degrades_if_store_fails_during_circuit_scan(
    tmp_path, monkeypatch
):
    store = SQLiteStateStore(tmp_path / "state.sqlite3")

    def fail_during_scan():
        raise sqlite3.OperationalError("database unavailable")

    monkeypatch.setattr(store, "_connect", fail_during_scan)
    decision = store.admit(_key(), now=100)

    assert decision.allowed is True
    assert decision.circuit_state is CircuitState.UNKNOWN
    assert decision.store_available is False
    assert decision.skip_reason is None


def test_reconcile_degrades_if_sqlite_fails_after_reservation(
    tmp_path, monkeypatch
):
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    store.configure_budget("request", "window", limit_units=3)
    assert store.reserve_budget("request", "window", units=1)

    def fail_connect():
        raise sqlite3.OperationalError("database disappeared")

    monkeypatch.setattr(store, "_connect", fail_connect)

    assert (
        store.reconcile_budget(
            "request", "window", reserved_units=1, actual_units=1
        )
        is False
    )
    assert store.available is False


def test_schema_initialization_is_idempotent_and_uses_wal(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = SQLiteStateStore(path)
    SQLiteStateStore(path)

    connection = sqlite3.connect(path)
    try:
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    finally:
        connection.close()

    assert mode.lower() == "wal"
    assert version == SCHEMA_VERSION

    no_checkpoint_on_close = getattr(
        sqlite3, "SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE", None
    )
    if no_checkpoint_on_close is not None and hasattr(connection, "getconfig"):
        owned_connection = store._connect()
        try:
            assert owned_connection.getconfig(no_checkpoint_on_close) is True
        finally:
            owned_connection.close()


def test_schema_initializes_once_per_path_and_reinitializes_after_deletion(
    tmp_path, monkeypatch
):
    path = tmp_path / "state.sqlite3"
    original = state_store_module.initialize_state_schema
    calls = 0

    def count_initialization(connection):
        nonlocal calls
        calls += 1
        original(connection)

    monkeypatch.setattr(state_store_module, "initialize_state_schema", count_initialization)
    SQLiteStateStore(path)
    SQLiteStateStore(path)
    assert calls == 1

    path.unlink()
    SQLiteStateStore(path)
    assert calls == 2
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='circuit_state'"
        ).fetchone() is not None


def test_a_replaced_database_file_is_initialized_again(tmp_path):
    # e.g. a restored backup or a migration in another process moved in place
    path = tmp_path / "state.sqlite3"
    SQLiteStateStore(path)
    replacement = tmp_path / "restored.sqlite3"
    sqlite3.connect(replacement).close()
    os.replace(replacement, path)

    store = SQLiteStateStore(path)

    assert store.available
    store.configure_budget("daily_provider_calls", "2026-10-08", limit_units=1)
    assert store.reserve_budget("daily_provider_calls", "2026-10-08", units=1)


def test_request_budget_is_in_memory_and_daily_budget_persists(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = SQLiteStateStore(path)
    store.configure_budget("request-1", "request", limit_units=2)
    assert store.reserve_budget("request-1", "request", units=1)
    assert store.reserve_budget("request-1", "request", units=1)
    assert not store.reserve_budget("request-1", "request", units=1)
    store.commit_budget("request-1", "request", units=1)
    assert store.get_budget("request-1", "request").used_units == 1
    assert store.read_budget_snapshot("request-1", "request") is not None
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM budget_ledger WHERE scope='request-1'"
        ).fetchone()[0] == 0

    store.configure_budget("daily_provider_calls", "2026-10-08", limit_units=3)
    assert store.reserve_budget("daily_provider_calls", "2026-10-08", units=1)
    store.commit_budget("daily_provider_calls", "2026-10-08", units=1)
    reopened = SQLiteStateStore(path)
    assert reopened.get_budget("daily_provider_calls", "2026-10-08").used_units == 1


def test_schema_v3_upgrades_v2_in_place_without_losing_existing_data(tmp_path):
    path = tmp_path / "state.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE budget_ledger (
                scope TEXT NOT NULL,
                window_key TEXT NOT NULL,
                limit_units INTEGER NOT NULL,
                used_units INTEGER NOT NULL DEFAULT 0,
                reserved_units INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (scope, window_key)
            )
            """
        )
        connection.execute(
            "INSERT INTO budget_ledger VALUES ('existing', 'window', 5, 2, 1)"
        )
        connection.execute("PRAGMA user_version=2")

    SQLiteStateStore(path)

    with sqlite3.connect(path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        row = connection.execute(
            "SELECT limit_units, used_units, reserved_units FROM budget_ledger"
        ).fetchone()
        tables = {
            item[0]
            for item in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }

    assert version == 3
    assert row == (5, 2, 1)
    assert "shadow_evaluations_v3" not in tables
