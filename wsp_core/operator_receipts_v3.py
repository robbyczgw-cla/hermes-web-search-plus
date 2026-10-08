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

try:  # POSIX only; Windows has no fcntl module.
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

from . import operator_privacy_v3 as privacy
from .contract_v3 import ResponseV3


JOURNAL_OWNER = "web-search-plus:operator-receipts-v3"
JOURNAL_SCHEMA_VERSION = 1
DEFAULT_TTL_SECONDS = 604800
DEFAULT_MAX_RECORDS = 1000
DEFAULT_MAX_BYTES = 8 * 1024 * 1024


def receipt_record_from_response(
    response: ResponseV3, *, timestamp: float | None = None
) -> dict[str, Any]:
    """Project a response into the fixed secret-free operator journal DTO."""
    limits_applied = {
        key: value
        for key, value in response.limits_applied.items()
        if isinstance(value, (int, float, bool))
    }
    extract_limits = response.limits_applied.get("extract")
    if isinstance(extract_limits, dict):
        limits_applied["extract"] = {
            key: extract_limits[key]
            for key in (
                "requested_url_count",
                "omitted_url_count",
                "max_urls",
                "max_context_chars",
                "context_chars_returned",
                "truncated",
            )
            if isinstance(extract_limits.get(key), (int, bool))
        }
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
        "limits_applied": limits_applied,
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

    @staticmethod
    def _open_lock(flags: int, directory_descriptor: int) -> int:
        """Create/open the lock file, retrying only a transient ENOENT.

        Concurrent first-use O_CREAT|O_NOFOLLOW can report ENOENT on macOS.
        Every other error (symlink, ownership, permissions) still fails closed,
        and the retry count is small and fixed.
        """
        attempts = 5
        for attempt in range(attempts):
            try:
                return os.open(".receipts.lock", flags, 0o600, dir_fd=directory_descriptor)
            except FileNotFoundError:
                if attempt == attempts - 1:
                    raise
                time.sleep(0.005 * (attempt + 1))
        raise AssertionError("unreachable")

    @contextmanager
    def _locked(self) -> Iterator[int]:
        with self._journal_directory() as directory_descriptor:
            flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = self._open_lock(flags, directory_descriptor)
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

    def _retained(
        self, records: list[dict[str, Any]], headroom: float = 1.0
    ) -> list[dict[str, Any]]:
        """Apply TTL and size limits.

        ``headroom`` < 1 trims a little below the limits on compaction, so the
        following appends take the O(1) path instead of rewriting every time
        once the journal is full.
        """
        cutoff = self.now() - self.ttl_seconds
        retained = [
            record
            for record in records
            if isinstance(record.get("timestamp"), (int, float))
            and not isinstance(record.get("timestamp"), bool)
            and float(record["timestamp"]) >= cutoff
        ]
        max_records = self.max_records
        max_bytes = self.max_bytes
        if headroom < 1.0 and len(retained) > max_records:
            max_records -= round(max_records * (1.0 - headroom))
        if self.max_records == 0:
            retained = []
        elif len(retained) > max_records:
            retained = retained[-max_records:]
        sizes = [len(encode_journal_record(record).encode("utf-8")) + 1 for record in retained]
        encoded_size = sum(sizes)
        start = 0
        if headroom < 1.0 and encoded_size > max_bytes:
            max_bytes -= round(max_bytes * (1.0 - headroom))
        while start < len(retained) and encoded_size > max_bytes:
            encoded_size -= sizes[start]
            start += 1
        return retained[start:]

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

    def _try_fast_append(
        self, line: str, record: dict[str, Any], directory_descriptor: int
    ) -> bool:
        """Append one line in place when no retention limit is reached.

        A search used to read, validate and rewrite the whole journal (cost grew
        with every stored receipt). Appending is O(1) in the common case; the
        full compaction below still runs whenever a limit would be crossed or the
        file is not plainly ours.
        """
        cutoff = self.now() - self.ttl_seconds
        stamp = record.get("timestamp")
        if (
            not isinstance(stamp, (int, float))
            or isinstance(stamp, bool)
            or float(stamp) < cutoff
        ):
            return False
        flags = os.O_RDWR | os.O_APPEND | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open("receipts.jsonl", flags, dir_fd=directory_descriptor)
        except FileNotFoundError:
            return False
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                return False
            data = b""
            if info.st_size:
                os.lseek(descriptor, 0, os.SEEK_SET)
                chunks = []
                while True:
                    chunk = os.read(descriptor, 1 << 20)
                    if not chunk:
                        break
                    chunks.append(chunk)
                data = b"".join(chunks)
                if not data.endswith(b"\n"):
                    return False
            encoded = (line + "\n").encode("utf-8")
            if data.count(b"\n") + 1 > self.max_records or len(data) + len(encoded) > self.max_bytes:
                return False
            if data:
                oldest = self._decode_owned_line(data.split(b"\n", 1)[0].decode("utf-8", "replace"))
                stamp = oldest.get("timestamp") if oldest else None
                if (
                    not isinstance(stamp, (int, float))
                    or isinstance(stamp, bool)
                    or float(stamp) < cutoff
                ):
                    return False
            os.write(descriptor, encoded)
            os.fsync(descriptor)
            return True
        finally:
            os.close(descriptor)

    def append(self, record: dict[str, Any]) -> bool:
        try:
            line = encode_journal_record(record)
            with self._locked() as directory_descriptor:
                if self.max_records and self._try_fast_append(line, record, directory_descriptor):
                    return True
                existing = self._read_all_owned(directory_descriptor)
                if existing is None:
                    return False
                retained = self._retained([*existing, dict(record)], headroom=0.9)
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
