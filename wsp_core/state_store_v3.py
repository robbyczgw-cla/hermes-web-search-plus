"""Fail-closed SQLite operational state for the Web Search Plus v3 engine."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .contract_v3 import Capability, CircuitState, ErrorClass, SkipReason


SCHEMA_VERSION = 3
DEFAULT_OPEN_SECONDS = {
    ErrorClass.AUTH: 300,
    ErrorClass.QUOTA: 3600,
    ErrorClass.RATE_LIMIT: 60,
    ErrorClass.TRANSIENT: 60,
    ErrorClass.TIMEOUT: 60,
}
# A single 5xx or timeout is usually a blip. Transient and timeout buckets
# block admission only after this many consecutive failures; a success
# deletes the bucket and resets the count. Auth, quota and rate-limit
# buckets still block on the first failure: they are not blips.
CONSECUTIVE_FAILURES_TO_OPEN = {
    ErrorClass.TRANSIENT: 3,
    ErrorClass.TIMEOUT: 3,
}

# Database files whose schema this process has initialized, keyed by path and
# inode: a deleted or replaced file (restore, another process's migration) is
# initialized again, as every store construction did before.
_INITIALIZED_STATE_FILES: set[tuple[str, int, int]] = set()
_INITIALIZED_STATE_FILES_LOCK = threading.Lock()


def _state_file_identity(path: Path) -> Optional[tuple[str, int, int]]:
    try:
        metadata = os.stat(path)
    except OSError:
        return None
    return (os.path.realpath(path), metadata.st_dev, metadata.st_ino)


def credential_fingerprint(
    secret: str | None, *, local_secret: bytes
) -> str:
    """Return an HMAC identity that is useless without the local state secret."""
    material = secret if secret else "<anonymous>"
    return hmac.new(
        local_secret, material.encode("utf-8"), hashlib.sha256
    ).hexdigest()[:24]


@dataclass(frozen=True)
class CircuitKey:
    provider: str
    capability: Capability
    endpoint: str
    credential_fingerprint: str

    def values(self) -> tuple[str, str, str, str]:
        return (
            self.provider,
            self.capability.value,
            self.endpoint,
            self.credential_fingerprint,
        )


@dataclass(frozen=True)
class CircuitRecord:
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    open_until: Optional[int] = None
    updated_at: int = 0


@dataclass(frozen=True)
class AdmissionDecision:
    allowed: bool
    circuit_state: CircuitState
    skip_reason: Optional[SkipReason] = None
    store_available: bool = True
    blocking_error_class: Optional[ErrorClass] = None
    # Transient and timeout buckets that hold failures below the open
    # threshold; a success must clear them or blips add up across requests.
    failing_error_classes: tuple[ErrorClass, ...] = ()


@dataclass(frozen=True)
class BudgetRecord:
    scope: str
    window_key: str
    limit_units: int
    used_units: int
    reserved_units: int


def initialize_state_schema(connection: sqlite3.Connection) -> None:
    """Create or upgrade the additive v3 operational-state schema."""
    connection.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS circuit_state (
            provider TEXT NOT NULL,
            capability TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            credential_fingerprint TEXT NOT NULL,
            error_class TEXT NOT NULL,
            state TEXT NOT NULL,
            failure_count INTEGER NOT NULL DEFAULT 0,
            open_until INTEGER,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (
                provider, capability, endpoint,
                credential_fingerprint, error_class
            )
        );
        CREATE TABLE IF NOT EXISTS budget_ledger (
            scope TEXT NOT NULL,
            window_key TEXT NOT NULL,
            limit_units INTEGER NOT NULL,
            used_units INTEGER NOT NULL DEFAULT 0,
            reserved_units INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (scope, window_key)
        );
        CREATE TABLE IF NOT EXISTS legacy_provider_health (
            provider TEXT PRIMARY KEY,
            failure_count INTEGER NOT NULL CHECK (failure_count >= 0),
            cooldown_until INTEGER NOT NULL CHECK (cooldown_until >= 0),
            cooldown_seconds INTEGER NOT NULL CHECK (cooldown_seconds >= 0),
            last_failure_at INTEGER NOT NULL CHECK (last_failure_at >= 0),
            source_digest TEXT NOT NULL,
            migrated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS adaptive_samples_v3 (
            provider TEXT NOT NULL,
            source_index INTEGER NOT NULL CHECK (source_index >= 0),
            sample_time INTEGER NOT NULL CHECK (sample_time >= 0),
            latency_ms INTEGER NOT NULL CHECK (latency_ms >= 0),
            result_count INTEGER NOT NULL CHECK (result_count >= 0),
            error INTEGER NOT NULL CHECK (error IN (0, 1)),
            source_digest TEXT NOT NULL,
            migrated_at INTEGER NOT NULL,
            PRIMARY KEY (provider, source_index)
        );
        CREATE INDEX IF NOT EXISTS adaptive_samples_v3_time
            ON adaptive_samples_v3(provider, sample_time);
        CREATE TABLE IF NOT EXISTS legacy_state_migrations (
            migration_id TEXT PRIMARY KEY,
            source_digest TEXT NOT NULL,
            applied_at INTEGER NOT NULL,
            health_providers INTEGER NOT NULL,
            adaptive_providers INTEGER NOT NULL,
            adaptive_samples INTEGER NOT NULL
        );
        PRAGMA user_version={SCHEMA_VERSION};
        """
    )


class SQLiteStateStore:
    """Durable policy state; persisted blocks fail closed, I/O loss degrades safely."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.secret_path = Path(f"{self.path}.secret")
        self._read_only = False
        self._local_secret = secrets.token_bytes(32)
        self._secret_available = False
        self._available = False
        self._request_budgets: dict[tuple[str, str], list[int]] = {}
        self._request_budgets_lock = threading.Lock()
        self._initialize_local_secret()
        self._initialize()
        if not self._secret_available:
            self._available = False

    @classmethod
    def open_readonly(cls, path: str | Path) -> "SQLiteStateStore":
        """Open an existing owned database for aggregate reads without writes."""
        store = cls.__new__(cls)
        store.path = Path(path)
        store.secret_path = Path(f"{store.path}.secret")
        store._read_only = True
        store._local_secret = b""
        store._secret_available = False
        store._available = False
        store._request_budgets = {}
        store._request_budgets_lock = threading.Lock()
        try:
            absolute = Path(os.path.abspath(os.fspath(store.path)))
            current = Path(os.path.sep)
            for component in absolute.parts[1:]:
                current /= component
                if stat.S_ISLNK(os.lstat(current).st_mode):
                    return store
            metadata = os.lstat(absolute)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
                return store
            connection = store._connect()
            connection.close()
            store._available = True
        except (OSError, sqlite3.Error):
            store._available = False
        return store

    def _initialize_local_secret(self) -> None:
        """Load or atomically create the DB-local HMAC key with mode 0600."""
        try:
            self.secret_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                fd = os.open(
                    self.secret_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                fd = None
            if fd is not None:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(self._local_secret)
            persisted = self.secret_path.read_bytes()
            if len(persisted) < 32:
                raise OSError("state secret is truncated")
            os.chmod(self.secret_path, 0o600)
            self._local_secret = persisted
            self._secret_available = True
        except OSError:
            # Ephemeral HMAC identity is safe; durable circuit state is unavailable.
            self._available = False

    def fingerprint_credential(self, credential: str | None) -> str:
        return credential_fingerprint(
            credential, local_secret=self._local_secret
        )

    @property
    def available(self) -> bool:
        return self._available

    def _connect(self) -> sqlite3.Connection:
        if self._read_only:
            connection = sqlite3.connect(
                f"{self.path.resolve().as_uri()}?mode=ro",
                uri=True,
                timeout=5,
                isolation_level=None,
            )
        else:
            connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        # This store intentionally uses short-lived WAL connections from
        # concurrent research workers.  SQLite normally checkpoints WAL when
        # the apparent last connection closes, which requires an exclusive
        # lock and can deadlock with another thread opening a connection.
        # Normal WAL auto-checkpointing still bounds the journal without doing
        # blocking persistence work during request teardown.
        no_checkpoint_on_close = getattr(
            sqlite3, "SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE", None
        )
        setconfig = getattr(connection, "setconfig", None)
        if no_checkpoint_on_close is not None and setconfig is not None:
            try:
                setconfig(no_checkpoint_on_close, True)
            except sqlite3.Error:
                # Older SQLite builds may expose the Python API without this
                # option; state access must retain its existing degradation.
                pass
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with _INITIALIZED_STATE_FILES_LOCK:
                if _state_file_identity(self.path) not in _INITIALIZED_STATE_FILES:
                    connection = self._connect()
                    try:
                        connection.execute("PRAGMA journal_mode=WAL")
                        connection.execute("PRAGMA foreign_keys=ON")
                        initialize_state_schema(connection)
                    finally:
                        connection.close()
                    identity = _state_file_identity(self.path)
                    if identity is not None:
                        _INITIALIZED_STATE_FILES.add(identity)
            self._available = True
        except (OSError, sqlite3.Error):
            self._available = False

    @staticmethod
    def _state_for(error_class: ErrorClass) -> CircuitState:
        if error_class is ErrorClass.AUTH:
            return CircuitState.BLOCKED_AUTH
        if error_class is ErrorClass.QUOTA:
            return CircuitState.BLOCKED_QUOTA
        return CircuitState.OPEN

    def record_failure(
        self,
        key: CircuitKey,
        error_class: ErrorClass,
        *,
        now: int,
        retry_after_seconds: float | None = None,
    ) -> CircuitRecord:
        if not self._available:
            return CircuitRecord(CircuitState.UNKNOWN)
        state = self._state_for(error_class)
        seconds = int(
            retry_after_seconds
            if retry_after_seconds is not None
            else DEFAULT_OPEN_SECONDS.get(error_class, 60)
        )
        open_until = now + max(1, seconds)
        try:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO circuit_state (
                        provider, capability, endpoint, credential_fingerprint,
                        error_class, state, failure_count, open_until, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT (
                        provider, capability, endpoint,
                        credential_fingerprint, error_class
                    ) DO UPDATE SET
                        state=excluded.state,
                        failure_count=circuit_state.failure_count + 1,
                        open_until=excluded.open_until,
                        updated_at=excluded.updated_at
                    """,
                    (*key.values(), error_class.value, state.value, open_until, now),
                )
                connection.commit()
            finally:
                connection.close()
        except sqlite3.Error:
            self._available = False
            return CircuitRecord(CircuitState.UNKNOWN)
        return self.get_circuit(key, error_class)

    def record_success(
        self, key: CircuitKey, error_class: ErrorClass, *, now: int
    ) -> None:
        del now
        if not self._available:
            return
        try:
            connection = self._connect()
            try:
                connection.execute(
                    """
                    DELETE FROM circuit_state
                    WHERE provider=? AND capability=? AND endpoint=?
                      AND credential_fingerprint=? AND error_class=?
                    """,
                    (*key.values(), error_class.value),
                )
            finally:
                connection.close()
        except sqlite3.Error:
            self._available = False

    def get_circuit(
        self, key: CircuitKey, error_class: ErrorClass
    ) -> CircuitRecord:
        if not self._available:
            return CircuitRecord(CircuitState.UNKNOWN)
        try:
            connection = self._connect()
            try:
                row = connection.execute(
                    """
                    SELECT state, failure_count, open_until, updated_at
                    FROM circuit_state
                    WHERE provider=? AND capability=? AND endpoint=?
                      AND credential_fingerprint=? AND error_class=?
                    """,
                    (*key.values(), error_class.value),
                ).fetchone()
            finally:
                connection.close()
        except sqlite3.Error:
            self._available = False
            return CircuitRecord(CircuitState.UNKNOWN)
        return self._circuit_record(row)

    @staticmethod
    def _circuit_record(row: Optional[sqlite3.Row]) -> CircuitRecord:
        if row is None:
            return CircuitRecord()
        return CircuitRecord(
            state=CircuitState(row["state"]),
            failure_count=int(row["failure_count"]),
            open_until=(int(row["open_until"]) if row["open_until"] is not None else None),
            updated_at=int(row["updated_at"]),
        )

    def _claim_half_open(
        self, key: CircuitKey, error_class: ErrorClass, *, now: int
    ) -> bool:
        """Atomically lease one probe for an expired circuit bucket."""
        if not self._available:
            return False
        try:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """
                    UPDATE circuit_state
                    SET state=?, open_until=?, updated_at=?
                    WHERE provider=? AND capability=? AND endpoint=?
                      AND credential_fingerprint=? AND error_class=?
                      AND state IN (?, ?, ?, ?)
                      AND open_until IS NOT NULL AND open_until <= ?
                    """,
                    (
                        CircuitState.HALF_OPEN.value,
                        now + 60,
                        now,
                        *key.values(),
                        error_class.value,
                        CircuitState.BLOCKED_AUTH.value,
                        CircuitState.BLOCKED_QUOTA.value,
                        CircuitState.OPEN.value,
                        CircuitState.HALF_OPEN.value,
                        now,
                    ),
                )
                connection.commit()
                return cursor.rowcount == 1
            finally:
                connection.close()
        except sqlite3.Error:
            self._available = False
            return False

    def admit(self, key: CircuitKey, *, now: int) -> AdmissionDecision:
        if not self._available:
            return AdmissionDecision(
                True,
                CircuitState.UNKNOWN,
                None,
                store_available=False,
            )
        checks = (
            (ErrorClass.AUTH, SkipReason.AUTH_BLOCKED),
            (ErrorClass.QUOTA, SkipReason.QUOTA_BLOCKED),
            (ErrorClass.RATE_LIMIT, SkipReason.RATE_LIMITED),
            (ErrorClass.TRANSIENT, SkipReason.CIRCUIT_OPEN),
            (ErrorClass.TIMEOUT, SkipReason.CIRCUIT_OPEN),
        )
        records = self._get_circuits(key, tuple(error for error, _ in checks))
        if not self._available:
            return AdmissionDecision(
                True, CircuitState.UNKNOWN, None, store_available=False
            )
        failing = tuple(
            error
            for error in CONSECUTIVE_FAILURES_TO_OPEN
            if records[error].failure_count > 0
        )
        expired = []
        for error_class, skip_reason in checks:
            record = records[error_class]
            if record.state is CircuitState.CLOSED:
                continue
            threshold = CONSECUTIVE_FAILURES_TO_OPEN.get(error_class, 1)
            if (
                record.state is CircuitState.OPEN
                and record.failure_count < threshold
            ):
                continue
            active = record.open_until is None or record.open_until > now
            if active:
                return AdmissionDecision(
                    False,
                    record.state,
                    skip_reason,
                    blocking_error_class=error_class,
                )
            expired.append((error_class, skip_reason))

        for error_class, skip_reason in expired:
            if self._claim_half_open(key, error_class, now=now):
                return AdmissionDecision(
                    True,
                    CircuitState.HALF_OPEN,
                    blocking_error_class=error_class,
                    failing_error_classes=failing,
                )
            if not self._available:
                return AdmissionDecision(
                    True,
                    CircuitState.UNKNOWN,
                    None,
                    store_available=False,
                )
            current = self.get_circuit(key, error_class)
            return AdmissionDecision(
                False,
                current.state,
                skip_reason,
                blocking_error_class=error_class,
            )
        return AdmissionDecision(
            True, CircuitState.CLOSED, failing_error_classes=failing
        )

    def _get_circuits(
        self, key: CircuitKey, error_classes: tuple[ErrorClass, ...]
    ) -> dict[ErrorClass, CircuitRecord]:
        """Fetch the circuit buckets for one admission with one SQLite query."""
        if not self._available:
            return {error: CircuitRecord(CircuitState.UNKNOWN) for error in error_classes}
        try:
            connection = self._connect()
            try:
                rows = connection.execute(
                    """
                    SELECT error_class, state, failure_count, open_until, updated_at
                    FROM circuit_state
                    WHERE provider=? AND capability=? AND endpoint=?
                      AND credential_fingerprint=?
                    """,
                    key.values(),
                ).fetchall()
            finally:
                connection.close()
        except sqlite3.Error:
            self._available = False
            return {error: CircuitRecord(CircuitState.UNKNOWN) for error in error_classes}
        by_error = {row["error_class"]: row for row in rows}
        return {error: self._circuit_record(by_error.get(error.value)) for error in error_classes}

    def configure_budget(
        self, scope: str, window_key: str, *, limit_units: int
    ) -> None:
        if limit_units < 0:
            raise ValueError("budget limit_units must be non-negative")
        if window_key == "request":
            with self._request_budgets_lock:
                budget = self._request_budgets.get((scope, window_key))
                if budget is None:
                    self._request_budgets[(scope, window_key)] = [limit_units, 0, 0]
                else:
                    budget[0] = limit_units
            return
        if not self._available:
            return
        try:
            connection = self._connect()
            try:
                connection.execute(
                    """
                    INSERT INTO budget_ledger (
                        scope, window_key, limit_units, used_units, reserved_units
                    ) VALUES (?, ?, ?, 0, 0)
                    ON CONFLICT(scope, window_key)
                    DO UPDATE SET limit_units=excluded.limit_units
                    """,
                    (scope, window_key, limit_units),
                )
            finally:
                connection.close()
        except sqlite3.Error:
            self._available = False

    def reserve_budget(self, scope: str, window_key: str, *, units: int) -> bool:
        if units < 0:
            raise ValueError("budget units must be non-negative")
        if window_key == "request":
            with self._request_budgets_lock:
                budget = self._request_budgets.get((scope, window_key))
                if budget is None or sum(budget[1:]) + units > budget[0]:
                    return False
                budget[2] += units
                return True
        if not self._available:
            return False
        try:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """
                    UPDATE budget_ledger
                    SET reserved_units=reserved_units + ?
                    WHERE scope=? AND window_key=?
                      AND used_units + reserved_units + ? <= limit_units
                    """,
                    (units, scope, window_key, units),
                )
                allowed = cursor.rowcount == 1
                connection.commit()
                return allowed
            finally:
                connection.close()
        except sqlite3.Error:
            self._available = False
            return False

    def release_budget(self, scope: str, window_key: str, *, units: int) -> bool:
        return self.reconcile_budget(
            scope, window_key, reserved_units=units, actual_units=0
        )

    def commit_budget(self, scope: str, window_key: str, *, units: int) -> bool:
        return self.reconcile_budget(
            scope, window_key, reserved_units=units, actual_units=units
        )

    def reconcile_budget(
        self,
        scope: str,
        window_key: str,
        *,
        reserved_units: int,
        actual_units: int,
    ) -> bool:
        """Atomically commit actual cost and release the unused reservation."""
        if reserved_units < 0 or actual_units < 0:
            raise ValueError("budget units must be non-negative")
        if actual_units > reserved_units:
            raise ValueError("actual budget units cannot exceed reserved units")
        if window_key == "request":
            with self._request_budgets_lock:
                budget = self._request_budgets.get((scope, window_key))
                if budget is None or budget[2] < reserved_units:
                    raise RuntimeError("budget reservation missing during reconciliation")
                budget[1] += actual_units
                budget[2] -= reserved_units
                return True
        if not self._available:
            return False
        try:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """
                    UPDATE budget_ledger
                    SET used_units=used_units + ?,
                        reserved_units=reserved_units - ?
                    WHERE scope=? AND window_key=?
                      AND reserved_units >= ?
                    """,
                    (
                        actual_units,
                        reserved_units,
                        scope,
                        window_key,
                        reserved_units,
                    ),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    raise RuntimeError("budget reservation missing during reconciliation")
                connection.commit()
                return True
            finally:
                connection.close()
        except sqlite3.Error:
            self._available = False
            return False

    def get_budget(self, scope: str, window_key: str) -> BudgetRecord:
        if window_key == "request":
            with self._request_budgets_lock:
                budget = self._request_budgets.get((scope, window_key))
                if budget is None:
                    raise KeyError((scope, window_key))
                return BudgetRecord(scope, window_key, *budget)
        if not self._available:
            raise RuntimeError("state store unavailable")
        try:
            connection = self._connect()
            try:
                row = connection.execute(
                    """
                    SELECT limit_units, used_units, reserved_units
                    FROM budget_ledger WHERE scope=? AND window_key=?
                    """,
                    (scope, window_key),
                ).fetchone()
            finally:
                connection.close()
        except sqlite3.Error as exc:
            self._available = False
            raise RuntimeError("state store unavailable") from exc
        if row is None:
            raise KeyError((scope, window_key))
        return BudgetRecord(
            scope,
            window_key,
            int(row["limit_units"]),
            int(row["used_units"]),
            int(row["reserved_units"]),
        )

    def read_budget_snapshot(
        self, scope: str, window_key: str
    ) -> BudgetRecord | None:
        """Read an optional ledger row without creating or updating state.

        Callers that require a zero-write read must obtain the store through
        :meth:`open_readonly`; a missing row is distinct from an unavailable
        database so policy can fail closed only when necessary.
        """
        if window_key == "request":
            with self._request_budgets_lock:
                budget = self._request_budgets.get((scope, window_key))
                return BudgetRecord(scope, window_key, *budget) if budget else None
        if not self._available:
            return None
        try:
            connection = self._connect()
            try:
                row = connection.execute(
                    """
                    SELECT limit_units, used_units, reserved_units
                    FROM budget_ledger WHERE scope=? AND window_key=?
                    """,
                    (scope, window_key),
                ).fetchone()
            finally:
                connection.close()
        except sqlite3.Error:
            self._available = False
            return None
        if row is None:
            return None
        return BudgetRecord(
            scope,
            window_key,
            int(row["limit_units"]),
            int(row["used_units"]),
            int(row["reserved_units"]),
        )

    def adaptive_sample_rows(self) -> list[tuple[str, int, int, int, int]]:
        """Return (provider, sample_time, latency_ms, result_count, error) rows."""
        if not self._available:
            return []
        try:
            connection = self._connect()
            try:
                rows = connection.execute(
                    """
                    SELECT provider, sample_time, latency_ms, result_count, error
                    FROM adaptive_samples_v3
                    ORDER BY provider, sample_time
                    """
                ).fetchall()
            finally:
                connection.close()
            return [
                (
                    str(row["provider"]),
                    int(row["sample_time"]),
                    int(row["latency_ms"]),
                    int(row["result_count"]),
                    int(row["error"]),
                )
                for row in rows
            ]
        except sqlite3.Error:
            self._available = False
            return []
