"""Marker-owned, bounded, privacy-safe WS-3 routing receipt journal."""

from __future__ import annotations

import json
import os
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

import fcntl

import operator_privacy_v3 as privacy
from contract_v3 import ResponseV3


JOURNAL_OWNER = "web-search-plus:operator-receipts-v3"
JOURNAL_SCHEMA_VERSION = 1
DEFAULT_TTL_SECONDS = 604800
DEFAULT_MAX_RECORDS = 1000
DEFAULT_MAX_BYTES = 8 * 1024 * 1024


def receipt_record_from_response(
    response: ResponseV3, *, timestamp: float | None = None
) -> dict[str, Any]:
    """Project a response into the fixed secret-free operator journal DTO."""
    record = {
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "timestamp": float(time.time() if timestamp is None else timestamp),
        "execution_id": response.execution_id,
        "capability": response.capability.value,
        "status": response.status.value,
        "routing_receipt": dict(response.routing_receipt),
        "cache": {
            "disposition": response.cache_status.get("disposition", "unavailable"),
            "origin_execution_id": response.cache_status.get("origin_execution_id"),
        },
        "current_provider_attempts": [
            provider_attempt.attempt_id
            for provider_attempt in response.provider_attempts
        ],
        "limits_applied": {
            key: value
            for key, value in response.limits_applied.items()
            if isinstance(value, (int, float, bool))
        },
        "warning_codes": [
            str(warning.get("code"))
            for warning in response.warnings
            if isinstance(warning, dict) and warning.get("code")
        ],
        "error_code": response.error.code if response.error else None,
    }
    privacy.assert_operator_payload_safe(record)
    return record


def encode_journal_record(record: dict[str, Any]) -> str:
    """Validate through the shared choke point and encode one owned JSONL line."""
    privacy.assert_operator_payload_safe(record)
    return json.dumps(
        {
            "owner": JOURNAL_OWNER,
            "journal_schema_version": JOURNAL_SCHEMA_VERSION,
            "payload": record,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


class OperatorReceiptJournal:
    def __init__(
        self,
        cache_root: str | Path,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        max_records: int = DEFAULT_MAX_RECORDS,
        max_bytes: int = DEFAULT_MAX_BYTES,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.cache_root = Path(os.path.abspath(os.fspath(cache_root)))
        self.path = self.cache_root / "operator" / "v3" / "receipts.jsonl"
        self.ttl_seconds = max(0, int(ttl_seconds))
        self.max_records = max(0, int(max_records))
        self.max_bytes = max(0, int(max_bytes))
        self.now = now

    @contextmanager
    def _journal_directory(self) -> Iterator[int]:
        """Open every path component without following symlinks."""
        directory_flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(os.path.sep, directory_flags)
        try:
            for component in self.path.parent.parts[1:]:
                try:
                    child = os.open(component, directory_flags, dir_fd=descriptor)
                except FileNotFoundError:
                    try:
                        os.mkdir(component, 0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                    child = os.open(component, directory_flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            directory_stat = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(directory_stat.st_mode)
                or directory_stat.st_uid != os.geteuid()
            ):
                raise OSError("operator journal directory is not owned regular storage")
            os.fchmod(descriptor, 0o700)
            yield descriptor
        finally:
            os.close(descriptor)

    @contextmanager
    def _locked(self) -> Iterator[int]:
        with self._journal_directory() as directory_descriptor:
            flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(
                ".receipts.lock",
                flags,
                0o600,
                dir_fd=directory_descriptor,
            )
            try:
                lock_stat = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(lock_stat.st_mode)
                    or lock_stat.st_uid != os.geteuid()
                ):
                    raise OSError("operator journal lock is not an owned regular file")
                os.fchmod(descriptor, 0o600)
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                try:
                    yield directory_descriptor
                finally:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    @staticmethod
    def _decode_owned_line(line: str) -> dict[str, Any] | None:
        try:
            envelope = json.loads(line)
        except (TypeError, ValueError):
            return None
        if (
            not isinstance(envelope, dict)
            or envelope.get("owner") != JOURNAL_OWNER
            or envelope.get("journal_schema_version") != JOURNAL_SCHEMA_VERSION
            or not isinstance(envelope.get("payload"), dict)
        ):
            return None
        payload = dict(envelope["payload"])
        try:
            privacy.assert_operator_payload_safe(payload)
        except ValueError:
            return None
        return payload

    def _read_all_owned(
        self, directory_descriptor: int
    ) -> list[dict[str, Any]] | None:
        try:
            path_stat = os.stat(
                "receipts.jsonl",
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return []
        if not stat.S_ISREG(path_stat.st_mode) or path_stat.st_uid != os.geteuid():
            return None
        flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        descriptor = -1
        try:
            descriptor = os.open(
                "receipts.jsonl",
                flags,
                dir_fd=directory_descriptor,
            )
            opened_stat = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened_stat.st_mode)
                or opened_stat.st_uid != os.geteuid()
                or (opened_stat.st_dev, opened_stat.st_ino)
                != (path_stat.st_dev, path_stat.st_ino)
            ):
                return None
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                descriptor = -1
                lines = handle.read().splitlines()
        except (OSError, UnicodeError):
            return None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        records = []
        for line in lines:
            if not line.strip():
                continue
            payload = self._decode_owned_line(line)
            if payload is None:
                return None
            records.append(payload)
        return records

    def _retained(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        cutoff = self.now() - self.ttl_seconds
        retained = [
            record
            for record in records
            if isinstance(record.get("timestamp"), (int, float))
            and not isinstance(record.get("timestamp"), bool)
            and float(record["timestamp"]) >= cutoff
        ]
        if self.max_records == 0:
            retained = []
        elif len(retained) > self.max_records:
            retained = retained[-self.max_records :]
        while retained:
            encoded_size = sum(
                len(encode_journal_record(record).encode("utf-8")) + 1
                for record in retained
            )
            if encoded_size <= self.max_bytes:
                break
            retained.pop(0)
        return retained

    def _rewrite(
        self,
        records: list[dict[str, Any]],
        directory_descriptor: int,
    ) -> None:
        temp_name = f".wsp-v3-receipts-{uuid.uuid4().hex}.tmp"
        content = "".join(f"{encode_journal_record(record)}\n" for record in records)
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(
                temp_name,
                flags,
                0o600,
                dir_fd=directory_descriptor,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(
                temp_name,
                "receipts.jsonl",
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
            )
            os.fsync(directory_descriptor)
        finally:
            try:
                os.unlink(temp_name, dir_fd=directory_descriptor)
            except FileNotFoundError:
                pass

    def append(self, record: dict[str, Any]) -> bool:
        try:
            encode_journal_record(record)
            with self._locked() as directory_descriptor:
                existing = self._read_all_owned(directory_descriptor)
                if existing is None:
                    return False
                retained = self._retained([*existing, dict(record)])
                self._rewrite(retained, directory_descriptor)
            return True
        except Exception:
            # Journal persistence is best-effort by contract.
            return False

    def load(self, limit: int = 100) -> list[dict[str, Any]]:
        try:
            with self._locked() as directory_descriptor:
                records = self._read_all_owned(directory_descriptor)
                if records is None:
                    return []
                retained = self._retained(records)
                if retained != records:
                    self._rewrite(retained, directory_descriptor)
        except OSError:
            return []
        newest = sorted(
            retained,
            key=lambda item: float(item.get("timestamp", 0)),
            reverse=True,
        )
        bounded_limit = max(1, min(int(limit), 100))
        return newest[:bounded_limit]
