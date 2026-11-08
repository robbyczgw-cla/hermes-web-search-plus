"""Circuit rows say what admission does; daily reservations have an owner.

Real SQLite state stores in a temp dir. Crashes are real child processes that
die with ``os._exit`` inside the provider call; no provider is contacted.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from wsp_core import state_store_v3
from wsp_core.attempt_engine_v3 import AttemptContext, AttemptEngine
from wsp_core.contract_v3 import Capability, CircuitState, ErrorClass
from wsp_core.http_client import ProviderRequestError
from wsp_core.operator_console_v3 import build_overview
from wsp_core.state_store_v3 import CircuitKey, SQLiteStateStore

ROOT = Path(__file__).resolve().parents[1]
DAILY = "daily_provider_calls"
WINDOW = "2026-11-08"


def _context(store: SQLiteStateStore, *, daily_limit: int | None = None) -> AttemptContext:
    daily = {}
    if daily_limit is not None:
        daily = {
            "daily_budget_scope": DAILY,
            "daily_budget_window": WINDOW,
            "daily_budget_limit_units": daily_limit,
        }
    return AttemptContext(
        provider="serper",
        capability=Capability.SEARCH,
        endpoint="https://google.serper.dev",
        credential_fingerprint=store.fingerprint_credential("k"),
        budget_scope="request-scope",
        budget_window="request",
        budget_limit_units=10,
        **daily,
    )


def _console_circuits(tmp_path: Path, state_path: Path) -> dict:
    # The console opens the file immutable and does not read the WAL.
    with sqlite3.connect(state_path) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return build_overview(
        cache_root=tmp_path / "cache",
        config={},
        provider_ids=[],
        state_path=state_path,
    )["circuits"]


def _raise(error: BaseException):
    def operation():
        raise error
    return operation


def _ok():
    return {"results": [{"url": "https://a.example"}]}


# --- circuit rows ---------------------------------------------------------


def test_healthy_provider_is_not_open_in_console_after_non_blocking_errors(tmp_path):
    state_path = tmp_path / "state.sqlite3"
    store = SQLiteStateStore(state_path)
    engine = AttemptEngine(store, max_attempts=1)
    context = _context(store)

    engine.execute(context, _raise(RuntimeError("adapter bug")), now=lambda: 1000)
    engine.execute(context, _raise(TypeError("bad shape")), now=lambda: 1001)
    blip = engine.execute(
        context, _raise(ProviderRequestError("x", status_code=503)), now=lambda: 1002
    )
    admitted = store.admit(context.circuit_key, now=1003)
    engine.execute(context, _ok, now=lambda: 1004)

    # One 503 does not block; the receipt says what admission says.
    assert admitted.allowed is True
    assert blip.receipt.circuit_state_after is admitted.circuit_state is CircuitState.CLOSED
    with sqlite3.connect(state_path) as connection:
        rows = connection.execute("SELECT error_class, state FROM circuit_state").fetchall()
    assert rows == []
    assert _console_circuits(tmp_path, state_path)["open"] == 0


def test_old_database_junk_rows_are_ignored_and_cleaned(tmp_path):
    state_path = tmp_path / "state.sqlite3"
    SQLiteStateStore(state_path)
    with sqlite3.connect(state_path) as connection:
        connection.executemany(
            "INSERT INTO circuit_state (provider, capability, endpoint, credential_fingerprint,"
            " error_class, state, failure_count, open_until, updated_at)"
            " VALUES ('serper', 'search', 'e', 'f', ?, 'open', ?, 99999999999, 1)",
            [("internal", 1), ("provider_contract", 1), ("config", 1), ("transient", 1),
             ("rate_limit", 1)],
        )
    # Console reads the old rows as they are (read-only): only the rate-limit
    # bucket blocks; the transient one is below its threshold.
    assert _console_circuits(tmp_path, state_path) == {
        "closed": 1, "open": 1, "blocked_auth": 0, "blocked_quota": 0, "unknown": 0,
    }
    state_store_v3._INITIALIZED_STATE_FILES.clear()
    SQLiteStateStore(state_path)  # the next process start migrates the file
    with sqlite3.connect(state_path) as connection:
        rows = sorted(connection.execute("SELECT error_class, state FROM circuit_state"))
    assert rows == [("rate_limit", "open"), ("transient", "closed")]


@pytest.mark.parametrize("error_class", [ErrorClass.TRANSIENT, ErrorClass.TIMEOUT])
def test_stored_state_matches_admission_below_and_at_threshold(tmp_path, error_class):
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    key = CircuitKey("brave", Capability.SEARCH, "e", "f")
    states = []
    for now in (100, 101, 102):
        record = store.record_failure(key, error_class, now=now)
        decision = store.admit(key, now=now)
        states.append((record.state, decision.allowed))
    assert states == [
        (CircuitState.CLOSED, True),
        (CircuitState.CLOSED, True),
        (CircuitState.OPEN, False),
    ]


def test_non_blocking_failure_is_not_written(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    key = CircuitKey("brave", Capability.SEARCH, "e", "f")
    for error_class in (ErrorClass.INTERNAL, ErrorClass.PROVIDER_CONTRACT, ErrorClass.CONFIG):
        assert store.record_failure(key, error_class, now=100).state is CircuitState.CLOSED
    assert store.get_circuit(key, ErrorClass.INTERNAL).failure_count == 0


def test_stale_probe_owner_cannot_release_a_newer_lease(tmp_path):
    """Sol F13: t=100 rate limit; probe A at 161; lease ends; probe B at 222;
    A's late release at 223 must not open a third probe at 224."""
    path = tmp_path / "state.sqlite3"
    first, second = SQLiteStateStore(path), SQLiteStateStore(path)
    key = CircuitKey("brave", Capability.SEARCH, "e", "f")
    first.record_failure(key, ErrorClass.RATE_LIMIT, now=100, retry_after_seconds=60)
    probe_a = first.admit(key, now=161)
    probe_b = second.admit(key, now=222)
    assert probe_a.allowed and probe_b.allowed
    assert probe_a.lease_token and probe_a.lease_token != probe_b.lease_token
    assert first.release_half_open(
        key, ErrorClass.RATE_LIMIT, now=223, lease_token=probe_a.lease_token
    ) is False
    assert second.admit(key, now=224).allowed is False
    assert second.release_half_open(
        key, ErrorClass.RATE_LIMIT, now=225, lease_token=probe_b.lease_token
    ) is True
    assert first.admit(key, now=226).allowed is True


# --- daily reservations ---------------------------------------------------

_CHILD = """
import os, sys
sys.path.insert(0, {root!r})
from wsp_core.attempt_engine_v3 import AttemptContext, AttemptEngine
from wsp_core.contract_v3 import Capability
from wsp_core.state_store_v3 import SQLiteStateStore
store = SQLiteStateStore({path!r})
context = AttemptContext(
    provider="serper", capability=Capability.SEARCH, endpoint="e",
    credential_fingerprint="f", budget_scope="child", budget_window="request",
    budget_limit_units=3, daily_budget_scope={scope!r},
    daily_budget_window={window!r}, daily_budget_limit_units={limit},
)
def operation():
    os._exit(137)  # host killed mid provider call (OOM, restart, kill -9)
AttemptEngine(store, max_attempts=1).execute(context, operation)
"""


def _crash_children(path: Path, count: int, *, limit: int = 100) -> None:
    code = _CHILD.format(root=str(ROOT), path=str(path), scope=DAILY, window=WINDOW, limit=limit)
    env = {**os.environ, "WSP_CACHE_DIR": str(path.parent)}
    for _ in range(count):
        assert subprocess.run([sys.executable, "-c", code], env=env).returncode == 137


def _ledger(path: Path) -> dict:
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM budget_ledger WHERE scope=? AND window_key=?", (DAILY, WINDOW)
        ).fetchone()
    return {k: row[k] for k in row.keys() if k.endswith("_units")}


def test_crashed_processes_leave_no_orphan_reservations(tmp_path):
    path = tmp_path / "state.sqlite3"
    _crash_children(path, 5)
    store = SQLiteStateStore(path)
    engine = AttemptEngine(store, max_attempts=1)

    execution = engine.execute(_context(store, daily_limit=100), _ok)

    assert execution.receipt.outcome.value == "success"
    # Reclaimed, not refunded: the dead calls may have been billed.
    assert _ledger(path) == {
        "limit_units": 100, "used_units": 6, "reserved_units": 0, "orphaned_units": 5,
    }
    with sqlite3.connect(path) as connection:
        phases = connection.execute(
            "SELECT phase, COUNT(*) FROM budget_reservations GROUP BY phase"
        ).fetchall()
    assert phases == [("orphan_charged", 5)]


def test_live_owner_in_another_store_is_not_reclaimed_until_expiry(tmp_path):
    path = tmp_path / "state.sqlite3"
    owner, other = SQLiteStateStore(path), SQLiteStateStore(path)
    for store in (owner, other):
        store.configure_budget(DAILY, WINDOW, limit_units=10)

    assert owner.reserve_budget(DAILY, WINDOW, units=1, now=1000)
    assert other.reserve_budget(DAILY, WINDOW, units=1, now=1001)
    assert _ledger(path)["reserved_units"] == 2  # same live pid: not an orphan

    # The owner hangs past the reservation lifetime; the other store reclaims
    # its unit as spent.
    late = 1000 + state_store_v3.RESERVATION_TTL_SECONDS
    assert other.reserve_budget(DAILY, WINDOW, units=1, now=late)
    assert _ledger(path) == {
        "limit_units": 10, "used_units": 1, "reserved_units": 2, "orphaned_units": 1,
    }
    # The hung owner finishes: its paid call is not counted twice.
    assert owner.commit_budget(DAILY, WINDOW, units=1)
    assert _ledger(path) == {
        "limit_units": 10, "used_units": 1, "reserved_units": 2, "orphaned_units": 0,
    }


def test_owner_that_never_dispatched_gets_its_reclaimed_unit_back(tmp_path):
    path = tmp_path / "state.sqlite3"
    owner, other = SQLiteStateStore(path), SQLiteStateStore(path)
    owner.configure_budget(DAILY, WINDOW, limit_units=10)
    owner.reserve_budget(DAILY, WINDOW, units=1, now=1000)
    other.reserve_budget(DAILY, WINDOW, units=1, now=1000 + state_store_v3.RESERVATION_TTL_SECONDS + 1)
    assert owner.release_budget(DAILY, WINDOW, units=1)
    assert _ledger(path) == {
        "limit_units": 10, "used_units": 0, "reserved_units": 1, "orphaned_units": 0,
    }


def test_v3_database_without_owner_columns_keeps_working(tmp_path):
    path = tmp_path / "state.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE budget_ledger (scope TEXT NOT NULL, window_key TEXT NOT NULL,"
            " limit_units INTEGER NOT NULL, used_units INTEGER NOT NULL DEFAULT 0,"
            " reserved_units INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (scope, window_key))"
        )
        connection.execute(
            "INSERT INTO budget_ledger VALUES (?, ?, 5, 1, 2)", (DAILY, WINDOW)
        )
        connection.execute("PRAGMA user_version=3")
    # Read-only consumers (preflight, console) see the old file unchanged.
    snapshot = SQLiteStateStore.open_readonly(path).read_budget_snapshot(DAILY, WINDOW)
    assert (snapshot.used_units, snapshot.reserved_units, snapshot.orphaned_units) == (1, 2, 0)

    store = SQLiteStateStore(path)
    assert store.reserve_budget(DAILY, WINDOW, units=1)
    assert store.commit_budget(DAILY, WINDOW, units=1)
    # Unowned units from before the upgrade stay counted: nobody can say
    # whether those calls were paid.
    assert _ledger(path) == {
        "limit_units": 5, "used_units": 2, "reserved_units": 2, "orphaned_units": 0,
    }
