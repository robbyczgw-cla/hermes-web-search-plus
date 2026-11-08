"""Fail-closed SQLite operational state for the Web Search Plus v3 engine."""

from __future__ import annotations

import hashlib
import hmac
import math
import os
import secrets
import socket
import sqlite3
import stat
import threading
import time
import uuid
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
# Longest circuit-open window taken from a provider's Retry-After; matches the
# legacy cooldown ceiling so a hostile or buggy header cannot park a provider.
MAX_OPEN_SECONDS = 3600
# A single 5xx or timeout is usually a blip. Transient and timeout buckets
# block admission only after this many consecutive failures; a success
# deletes the bucket and resets the count. Auth, quota and rate-limit
# buckets still block on the first failure: they are not blips.
CONSECUTIVE_FAILURES_TO_OPEN = {
    ErrorClass.TRANSIENT: 3,
    ErrorClass.TIMEOUT: 3,
}
# Only these error classes can block admission, so only they own circuit
# rows. Internal, contract and config errors say nothing about provider
# health; a row for them would never be read or cleared.
BLOCKING_ERROR_CLASSES = (
    ErrorClass.AUTH,
    ErrorClass.QUOTA,
    ErrorClass.RATE_LIMIT,
    ErrorClass.TRANSIENT,
    ErrorClass.TIMEOUT,
)

# A daily reservation whose owner is gone (dead process on this host) or that
# outlived this many seconds is reclaimed. Reclaiming charges the units as
# used: the provider call may have been sent and billed, so it is never
# refunded unless its owner comes back and says it was not dispatched.
RESERVATION_TTL_SECONDS = 600
# Reclaimed reservations are kept this long so a late owner can still settle.
RECLAIMED_RESERVATION_RETENTION_SECONDS = 2 * 86400
_HOSTNAME = socket.gethostname()

# After an I/O error the store reports itself unavailable; it is probed again
# once this many seconds have passed, so one transient error (a locked
# database, a full disk that was cleaned) does not disable durable state for
# the rest of the store's lifetime.
RECOVERY_PROBE_SECONDS = 30.0

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
    # Identifies this caller's half-open probe lease; only its holder may
    # give it back.
    lease_token: Optional[str] = None
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
    # Part of used_units: reservations of dead or hung callers, charged
    # because nobody can say whether their provider call was billed.
    orphaned_units: int = 0


def effective_circuit_state(
    error_class: ErrorClass, state: CircuitState, failure_count: int
) -> CircuitState:
    """The state admission acts on; rows written before 5.1.8 stored ``open``
    for buckets still below their consecutive-failure threshold."""
    if (
        state is CircuitState.OPEN
        and failure_count < CONSECUTIVE_FAILURES_TO_OPEN.get(error_class, 1)
    ):
        return CircuitState.CLOSED
    return state


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _owner_alive(host: str, pid: int) -> bool:
    """Whether a reservation owner may still be running; unknown counts as alive."""
    if host != _HOSTNAME or pid == os.getpid() or os.name == "nt":
        # Other hosts are invisible and os.kill on Windows terminates the
        # process: the reservation lifetime decides there.
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def initialize_state_schema(connection: sqlite3.Connection) -> None:
    """Create or upgrade the additive v3 operational-state schema."""
    connection.executescript(
        """
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
        CREATE TABLE IF NOT EXISTS budget_reservations (
            reservation_id TEXT PRIMARY KEY,
            scope TEXT NOT NULL,
            window_key TEXT NOT NULL,
            units INTEGER NOT NULL CHECK (units >= 0),
            owner_host TEXT NOT NULL,
            owner_pid INTEGER NOT NULL,
            owner_token TEXT NOT NULL,
            phase TEXT NOT NULL CHECK (phase IN ('reserved', 'orphan_charged')),
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            settled_at INTEGER
        );
        CREATE INDEX IF NOT EXISTS budget_reservations_window
            ON budget_reservations(scope, window_key, phase);
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
        """
    )
    # Additive (5.1.8): owners for daily reservations, probe-lease tokens, and
    # circuit rows that store the state admission acts on.
    blocking = tuple(error.value for error in BLOCKING_ERROR_CLASSES)
    connection.execute("BEGIN IMMEDIATE")
    try:
        if "orphaned_units" not in _columns(connection, "budget_ledger"):
            connection.execute(
                "ALTER TABLE budget_ledger"
                " ADD COLUMN orphaned_units INTEGER NOT NULL DEFAULT 0"
            )
        if "lease_token" not in _columns(connection, "circuit_state"):
            connection.execute("ALTER TABLE circuit_state ADD COLUMN lease_token TEXT")
        connection.execute(
            "DELETE FROM circuit_state WHERE error_class NOT IN"
            f" ({', '.join('?' for _ in blocking)})",
            blocking,
        )
        for error_class, threshold in CONSECUTIVE_FAILURES_TO_OPEN.items():
            connection.execute(
                "UPDATE circuit_state SET state=?"
                " WHERE error_class=? AND state=? AND failure_count < ?",
                (
                    CircuitState.CLOSED.value,
                    error_class.value,
                    CircuitState.OPEN.value,
                    threshold,
                ),
            )
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


class SQLiteStateStore:
    """Durable policy state; persisted blocks fail closed, I/O loss degrades safely."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.secret_path = Path(f"{self.path}.secret")
        self._read_only = False
        self._local_secret = secrets.token_bytes(32)
        self._secret_available = False
        self._available_flag = False
        self._unavailable_since: float | None = None
        self._probe_lock = threading.Lock()
        self._available = False
        self._request_budgets: dict[tuple[str, str], list[int]] = {}
        self._request_budgets_lock = threading.Lock()
        self._owner_token = secrets.token_hex(8)
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
        store._available_flag = False
        store._unavailable_since = None
        store._probe_lock = threading.Lock()
        store._available = False
        store._request_budgets = {}
        store._request_budgets_lock = threading.Lock()
        store._owner_token = secrets.token_hex(8)
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
    def _available(self) -> bool:
        """Whether durable state is usable; probes again after an error window."""
        if self._available_flag:
            return True
        since = self._unavailable_since
        if (
            self._read_only
            or since is None
            or time.monotonic() - since < RECOVERY_PROBE_SECONDS
        ):
            return False
        return self._reprobe()

    @_available.setter
    def _available(self, value: bool) -> None:
        self._available_flag = bool(value)
        self._unavailable_since = None if value else time.monotonic()

    def _reprobe(self) -> bool:
        """Try to bring the store back after an earlier error; rate limited."""
        with self._probe_lock:
            if self._available_flag:
                return True
            if time.monotonic() - (self._unavailable_since or 0.0) < RECOVERY_PROBE_SECONDS:
                return False
            # Restart the quiet period first so a failing probe is not repeated
            # on every call.
            self._unavailable_since = time.monotonic()
            if not self._secret_available:
                self._initialize_local_secret()
            if not self._secret_available:
                return False
            self._initialize()
            if self._available_flag:
                # _initialize skips already-initialized files; confirm the file
                # is really readable again before trusting it.
                try:
                    connection = self._connect()
                    try:
                        connection.execute(
                            "SELECT 1 FROM circuit_state LIMIT 1"
                        ).fetchall()
                    finally:
                        connection.close()
                except sqlite3.Error:
                    self._available = False
            return self._available_flag

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
        if error_class not in BLOCKING_ERROR_CLASSES:
            # Not a health signal: nothing is written, nothing is blocked.
            return CircuitRecord()
        if not self._available:
            return CircuitRecord(CircuitState.UNKNOWN)
        state = self._state_for(error_class)
        threshold = CONSECUTIVE_FAILURES_TO_OPEN.get(error_class, 1)
        if retry_after_seconds is not None and math.isfinite(retry_after_seconds):
            seconds = int(min(retry_after_seconds, MAX_OPEN_SECONDS))
        else:
            seconds = DEFAULT_OPEN_SECONDS.get(error_class, 60)
        open_until = now + min(max(1, seconds), MAX_OPEN_SECONDS)
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
                        state=CASE
                            WHEN circuit_state.failure_count + 1 >= ?
                            THEN ? ELSE ? END,
                        failure_count=circuit_state.failure_count + 1,
                        open_until=excluded.open_until,
                        updated_at=excluded.updated_at,
                        lease_token=NULL
                    """,
                    (
                        *key.values(),
                        error_class.value,
                        (state if threshold <= 1 else CircuitState.CLOSED).value,
                        open_until,
                        now,
                        threshold,
                        state.value,
                        CircuitState.CLOSED.value,
                    ),
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
    ) -> Optional[str]:
        """Atomically lease one probe for an expired circuit bucket.

        Returns the lease token, or None when another caller holds the probe.
        """
        if not self._available:
            return None
        token = secrets.token_hex(8)
        try:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """
                    UPDATE circuit_state
                    SET state=?, open_until=?, updated_at=?, lease_token=?
                    WHERE provider=? AND capability=? AND endpoint=?
                      AND credential_fingerprint=? AND error_class=?
                      AND state IN (?, ?, ?, ?)
                      AND open_until IS NOT NULL AND open_until <= ?
                    """,
                    (
                        CircuitState.HALF_OPEN.value,
                        now + 60,
                        now,
                        token,
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
                return token if cursor.rowcount == 1 else None
            finally:
                connection.close()
        except sqlite3.Error:
            self._available = False
            return None

    def expire_circuit(
        self, key: CircuitKey, error_class: ErrorClass, *, now: int
    ) -> bool:
        """End an open window early so the next admission may probe.

        The failure count is kept: a probe that fails again re-opens the
        circuit at once, and only a successful probe clears it.
        """
        if not self._available:
            return False
        try:
            connection = self._connect()
            try:
                cursor = connection.execute(
                    """
                    UPDATE circuit_state SET open_until=?, updated_at=?
                    WHERE provider=? AND capability=? AND endpoint=?
                      AND credential_fingerprint=? AND error_class=?
                      AND state=? AND open_until > ?
                    """,
                    (
                        now,
                        now,
                        *key.values(),
                        error_class.value,
                        CircuitState.OPEN.value,
                        now,
                    ),
                )
                return cursor.rowcount == 1
            finally:
                connection.close()
        except sqlite3.Error:
            self._available = False
            return False

    def release_half_open(
        self,
        key: CircuitKey,
        error_class: ErrorClass,
        *,
        now: int,
        lease_token: Optional[str] = None,
    ) -> bool:
        """Give back a probe lease that was claimed but never used.

        With ``lease_token`` only the current holder may release: a caller
        whose lease already expired and was handed to a newer probe must not
        open the bucket for a third one.
        """
        if not self._available:
            return False
        try:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """
                    UPDATE circuit_state SET open_until=?, updated_at=?
                    WHERE provider=? AND capability=? AND endpoint=?
                      AND credential_fingerprint=? AND error_class=?
                      AND state=? AND (? IS NULL OR lease_token=?)
                    """,
                    (
                        now,
                        now,
                        *key.values(),
                        error_class.value,
                        CircuitState.HALF_OPEN.value,
                        lease_token,
                        lease_token,
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
        # Same classes as BLOCKING_ERROR_CLASSES, in blocking priority.
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
            if effective_circuit_state(
                error_class, record.state, record.failure_count
            ) is CircuitState.CLOSED:
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
            lease_token = self._claim_half_open(key, error_class, now=now)
            if lease_token is not None:
                return AdmissionDecision(
                    True,
                    CircuitState.HALF_OPEN,
                    blocking_error_class=error_class,
                    lease_token=lease_token,
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

    def _reclaim_orphans(
        self, connection: sqlite3.Connection, scope: str, window_key: str, now: int
    ) -> None:
        """Charge reservations of dead or expired owners as used (in a transaction).

        A crashed caller may have sent its provider request, so its units stay
        spent; they move from ``reserved_units`` to ``used_units`` and are
        counted in ``orphaned_units`` instead of looking in flight all day.
        """
        rows = connection.execute(
            """
            SELECT reservation_id, units, owner_host, owner_pid, expires_at
            FROM budget_reservations
            WHERE scope=? AND window_key=? AND phase='reserved'
            """,
            (scope, window_key),
        ).fetchall()
        orphans = [
            row
            for row in rows
            if int(row["expires_at"]) <= now
            or not _owner_alive(str(row["owner_host"]), int(row["owner_pid"]))
        ]
        units = sum(int(row["units"]) for row in orphans)
        if orphans:
            cursor = connection.execute(
                """
                UPDATE budget_ledger
                SET used_units=used_units + ?,
                    reserved_units=reserved_units - ?,
                    orphaned_units=orphaned_units + ?
                WHERE scope=? AND window_key=? AND reserved_units >= ?
                """,
                (units, units, units, scope, window_key, units),
            )
            if cursor.rowcount == 1:
                connection.executemany(
                    """
                    UPDATE budget_reservations
                    SET phase='orphan_charged', settled_at=?
                    WHERE reservation_id=?
                    """,
                    [(now, row["reservation_id"]) for row in orphans],
                )
        connection.execute(
            """
            DELETE FROM budget_reservations
            WHERE phase='orphan_charged' AND settled_at < ?
            """,
            (now - RECLAIMED_RESERVATION_RETENTION_SECONDS,),
        )

    def reserve_budget(
        self,
        scope: str,
        window_key: str,
        *,
        units: int,
        now: int | None = None,
    ) -> bool:
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
        current = int(time.time()) if now is None else int(now)
        try:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._reclaim_orphans(connection, scope, window_key, current)
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
                if allowed:
                    connection.execute(
                        """
                        INSERT INTO budget_reservations (
                            reservation_id, scope, window_key, units,
                            owner_host, owner_pid, owner_token, phase,
                            created_at, expires_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?)
                        """,
                        (
                            uuid.uuid4().hex,
                            scope,
                            window_key,
                            units,
                            _HOSTNAME,
                            os.getpid(),
                            self._owner_token,
                            current,
                            current + RESERVATION_TTL_SECONDS,
                        ),
                    )
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
                own = connection.execute(
                    """
                    SELECT reservation_id, phase FROM budget_reservations
                    WHERE scope=? AND window_key=? AND owner_token=?
                      AND owner_pid=? AND units=?
                    ORDER BY created_at LIMIT 1
                    """,
                    (scope, window_key, self._owner_token, os.getpid(), reserved_units),
                ).fetchone()
                if own is not None and own["phase"] == "orphan_charged":
                    # Reclaimed while this call was still running and already
                    # charged in full; only an undispatched call gets it back.
                    cursor = connection.execute(
                        """
                        UPDATE budget_ledger
                        SET used_units=used_units - ?,
                            orphaned_units=orphaned_units - ?
                        WHERE scope=? AND window_key=?
                          AND used_units >= ? AND orphaned_units >= ?
                        """,
                        (
                            reserved_units - actual_units,
                            reserved_units,
                            scope,
                            window_key,
                            reserved_units - actual_units,
                            reserved_units,
                        ),
                    )
                else:
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
                if own is not None and cursor.rowcount == 1:
                    connection.execute(
                        "DELETE FROM budget_reservations WHERE reservation_id=?",
                        (own["reservation_id"],),
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

    @staticmethod
    def _budget_record(scope: str, window_key: str, row: sqlite3.Row) -> BudgetRecord:
        # Read-only consumers may open a pre-5.1.8 file without orphaned_units.
        orphaned = row["orphaned_units"] if "orphaned_units" in row.keys() else 0
        return BudgetRecord(
            scope,
            window_key,
            int(row["limit_units"]),
            int(row["used_units"]),
            int(row["reserved_units"]),
            int(orphaned or 0),
        )

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
                    "SELECT * FROM budget_ledger WHERE scope=? AND window_key=?",
                    (scope, window_key),
                ).fetchone()
            finally:
                connection.close()
        except sqlite3.Error as exc:
            self._available = False
            raise RuntimeError("state store unavailable") from exc
        if row is None:
            raise KeyError((scope, window_key))
        return self._budget_record(scope, window_key, row)

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
                    "SELECT * FROM budget_ledger WHERE scope=? AND window_key=?",
                    (scope, window_key),
                ).fetchone()
            finally:
                connection.close()
        except sqlite3.Error:
            self._available = False
            return None
        if row is None:
            return None
        return self._budget_record(scope, window_key, row)

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
