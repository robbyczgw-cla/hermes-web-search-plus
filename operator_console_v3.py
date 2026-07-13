"""Read-only, privacy-safe snapshot backend for the WS-3 Operator Console."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

import operator_privacy_v3 as privacy
from bounded_context_v3 import (
    DEFAULT_FULL_TEXT_MAX_BYTES,
    DEFAULT_FULL_TEXT_TTL_SECONDS,
    DEFAULT_MAX_CONTEXT_CHARS,
    DEFAULT_MAX_URLS,
    HARD_MAX_URLS,
    MAX_CONTEXT_CHARS,
    MIN_CONTEXT_CHARS,
)
from config import DEFAULT_CONFIG, get_api_key, provider_configured
from operator_receipts_v3 import JOURNAL_OWNER, JOURNAL_SCHEMA_VERSION
from provider_registry import PROVIDER_SPECS


SNAPSHOT_SCHEMA_VERSION = 1
BENCHMARK_OWNER = "web-search-plus:operator-benchmarks-v3"
BENCHMARK_HISTORY_SCHEMA_VERSION = 1
MAX_ENDPOINT_LIMIT = 100
MAX_READ_BYTES = 8 * 1024 * 1024


def _bounded_limit(value: int) -> int:
    if isinstance(value, bool):
        return 1
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 100
    return max(1, min(parsed, MAX_ENDPOINT_LIMIT))


def _has_symlink_component(path: Path) -> bool:
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(os.path.sep)
    for component in absolute.parts[1:]:
        current /= component
        try:
            if stat.S_ISLNK(os.lstat(current).st_mode):
                return True
        except FileNotFoundError:
            return False
        except OSError:
            return True
    return False


def serialize_endpoint_payload(
    payload: Mapping[str, Any], *, configured_secrets: Sequence[str] = ()
) -> bytes:
    """Apply the one privacy choke point and produce deterministic JSON bytes."""
    if configured_secrets:
        privacy.assert_operator_payload_safe(
            payload, configured_secrets=configured_secrets
        )
    else:
        privacy.assert_operator_payload_safe(payload)
    return (
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")


@contextmanager
def _open_existing_directory(path: Path) -> Iterator[int]:
    """Open an absolute directory path without following any symlink component."""
    absolute = Path(os.path.abspath(os.fspath(path)))
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(os.path.sep, flags)
    try:
        for component in absolute.parts[1:]:
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode) or opened.st_uid != os.geteuid():
            raise OSError("operator snapshot directory is not owned regular storage")
        yield descriptor
    finally:
        os.close(descriptor)


def _read_owned_jsonl(
    path: Path,
    *,
    owner: str,
    version_field: str,
    version: int,
) -> list[dict[str, Any]]:
    """Read one bounded owned JSONL file without creating, locking or rewriting it."""
    descriptor = -1
    try:
        with _open_existing_directory(path.parent) as directory_descriptor:
            path_stat = os.stat(path.name, dir_fd=directory_descriptor, follow_symlinks=False)
            if (
                not stat.S_ISREG(path_stat.st_mode)
                or path_stat.st_uid != os.geteuid()
                or path_stat.st_size > MAX_READ_BYTES
            ):
                return []
            flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path.name, flags, dir_fd=directory_descriptor)
            opened_stat = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened_stat.st_mode)
                or opened_stat.st_uid != os.geteuid()
                or (opened_stat.st_dev, opened_stat.st_ino)
                != (path_stat.st_dev, path_stat.st_ino)
            ):
                return []
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                descriptor = -1
                lines = handle.read(MAX_READ_BYTES + 1).splitlines()
    except (FileNotFoundError, NotADirectoryError, OSError, UnicodeError):
        return []
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    records: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            envelope = json.loads(line)
        except (TypeError, ValueError):
            return []
        if (
            not isinstance(envelope, dict)
            or envelope.get("owner") != owner
            or envelope.get(version_field) != version
            or not isinstance(envelope.get("payload"), dict)
        ):
            return []
        payload = dict(envelope["payload"])
        try:
            privacy.assert_operator_payload_safe(payload)
        except ValueError:
            return []
        records.append(payload)
    return records


def _project_receipt(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": int(record.get("schema_version", 1)),
        "timestamp": float(record["timestamp"]),
        "execution_id": record["execution_id"],
        "capability": record["capability"],
        "status": record["status"],
        "routing": dict(record.get("routing_receipt") or {}),
        "current_provider_attempts": list(record.get("current_provider_attempts") or []),
        "cache": dict(record.get("cache") or {}),
        "limits": dict(record.get("limits_applied") or {}),
        "warning_codes": list(record.get("warning_codes") or []),
        "error_code": record.get("error_code"),
    }


def build_receipts(*, cache_root: str | Path, limit: int = 100) -> dict[str, Any]:
    path = Path(cache_root) / "operator" / "v3" / "receipts.jsonl"
    records = _read_owned_jsonl(
        path,
        owner=JOURNAL_OWNER,
        version_field="journal_schema_version",
        version=JOURNAL_SCHEMA_VERSION,
    )
    newest = sorted(
        records,
        key=lambda item: float(item.get("timestamp", 0)),
        reverse=True,
    )[: _bounded_limit(limit)]
    payload = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "receipts": [_project_receipt(record) for record in newest],
    }
    privacy.assert_operator_payload_safe(payload)
    return payload


def _project_benchmark(record: Mapping[str, Any]) -> dict[str, Any] | None:
    kind = record.get("kind") or "search"
    if kind not in {"search", "extract"}:
        return None
    providers = []
    for raw in record.get("providers") or []:
        if not isinstance(raw, Mapping) or raw.get("provider") not in PROVIDER_SPECS:
            continue
        errors = raw.get("errors")
        providers.append(
            {
                "provider": raw["provider"],
                "score": float(raw.get("score", 0.0)),
                "success_rate": float(raw.get("success_rate", 0.0)),
                "median_latency_seconds": (
                    None
                    if raw.get("median_latency_seconds") is None
                    else float(raw["median_latency_seconds"])
                ),
                "error_count": int(
                    raw.get("error_count", len(errors) if isinstance(errors, list) else 0)
                ),
            }
        )
    recommendation = record.get("recommended_priority")
    if recommendation is None and isinstance(record.get("recommendation"), Mapping):
        recommendation = record["recommendation"].get("provider_priority")
    priority = [item for item in (recommendation or []) if item in PROVIDER_SPECS]
    timestamp = record.get("timestamp")
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
        return None
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "kind": kind,
        "timestamp": float(timestamp),
        "status": "completed",
        "ok": bool(record.get("ok")),
        "providers": providers,
        "recommended_priority": priority,
    }


def build_benchmark_history(
    *, cache_root: str | Path, limit: int = 100
) -> dict[str, Any]:
    path = Path(cache_root) / "operator" / "v3" / "benchmark-history.jsonl"
    raw_records = _read_owned_jsonl(
        path,
        owner=BENCHMARK_OWNER,
        version_field="history_schema_version",
        version=BENCHMARK_HISTORY_SCHEMA_VERSION,
    )
    projected = [item for raw in raw_records if (item := _project_benchmark(raw))]
    runs = sorted(projected, key=lambda item: item["timestamp"], reverse=True)[
        : _bounded_limit(limit)
    ]
    kinds = {run["kind"] for run in runs}
    payload = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "runs": runs,
        "availability": {
            "search": "collected" if "search" in kinds else "not_collected",
            "extract": "collected" if "extract" in kinds else "not_collected",
        },
    }
    privacy.assert_operator_payload_safe(payload)
    return payload


def _provider_rows(
    config: Mapping[str, Any],
    provider_ids: Sequence[str],
    *,
    cache_root: Path,
    now: float,
) -> list[dict[str, Any]]:
    auto = config.get("auto_routing")
    auto = auto if isinstance(auto, Mapping) else {}
    disabled = set(auto.get("disabled_providers") or [])
    allowed_override = auto.get("allowed_providers")
    allowed = set(allowed_override) if isinstance(allowed_override, list) else None
    health_path = cache_root / "provider_health.json"
    health: Mapping[str, Any] = {}
    try:
        if (
            not _has_symlink_component(health_path)
            and health_path.stat().st_size <= MAX_READ_BYTES
        ):
            loaded = json.loads(health_path.read_text(encoding="utf-8"))
            if isinstance(loaded, Mapping):
                health = loaded
    except (OSError, TypeError, ValueError):
        health = {}

    rows = []
    for provider in provider_ids:
        spec = PROVIDER_SPECS.get(provider)
        if spec is None or spec.rejected_reason is not None:
            continue
        capabilities = []
        if spec.supports_search:
            capabilities.append("search")
        if spec.supports_extract:
            capabilities.append("extract")
        pstate = health.get(provider)
        pstate = pstate if isinstance(pstate, Mapping) else {}
        rows.append(
            {
                "provider": provider,
                "display_name": spec.display_name,
                "capabilities": capabilities,
                "configured": provider_configured(provider, dict(config)),
                "key_present": bool(get_api_key(provider, dict(config))),
                "disabled": provider in disabled,
                "auto_allowed": (
                    provider in allowed if allowed is not None else spec.auto_allowed_by_default
                ),
                "cooldown_active": float(pstate.get("cooldown_until", 0) or 0) > now,
            }
        )
    return rows


def _cache_snapshot(cache_root: Path) -> dict[str, Any]:
    response_entries = response_bytes = full_text_entries = full_text_bytes = 0
    timestamps: list[float] = []
    if _has_symlink_component(cache_root):
        return {
            "response_entries": 0,
            "response_bytes": 0,
            "full_text_entries": 0,
            "full_text_bytes": 0,
            "oldest_timestamp": None,
            "newest_timestamp": None,
        }
    response_root = cache_root / "v3" / "response"
    if response_root.is_dir() and not response_root.is_symlink():
        for path in response_root.rglob("*.json"):
            try:
                if path.is_symlink() or path.stat().st_size > MAX_READ_BYTES:
                    continue
                envelope = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(envelope, Mapping) or envelope.get("owner") != "web-search-plus:v3":
                    continue
                item_stat = path.stat()
                response_entries += 1
                response_bytes += item_stat.st_size
                created = envelope.get("created_at")
                timestamps.append(float(created) if isinstance(created, (int, float)) else item_stat.st_mtime)
            except (OSError, TypeError, ValueError):
                continue
    full_text_root = cache_root / "web" / "v3"
    if full_text_root.is_dir() and not full_text_root.is_symlink():
        for path in full_text_root.glob("*.md"):
            try:
                if path.is_symlink():
                    continue
                with path.open("r", encoding="utf-8") as handle:
                    if not handle.readline().startswith("<!-- wsp:web_text_v3 "):
                        continue
                item_stat = path.stat()
                full_text_entries += 1
                full_text_bytes += item_stat.st_size
                timestamps.append(item_stat.st_mtime)
            except (OSError, UnicodeError):
                continue
    return {
        "response_entries": response_entries,
        "response_bytes": response_bytes,
        "full_text_entries": full_text_entries,
        "full_text_bytes": full_text_bytes,
        "oldest_timestamp": min(timestamps) if timestamps else None,
        "newest_timestamp": max(timestamps) if timestamps else None,
    }


def _circuit_snapshot(state_path: Path) -> tuple[bool, dict[str, int]]:
    counts = {"closed": 0, "open": 0, "blocked_auth": 0, "blocked_quota": 0, "unknown": 0}
    if _has_symlink_component(state_path) or not state_path.is_file():
        return False, counts
    connection: sqlite3.Connection | None = None
    try:
        uri = f"file:{state_path.resolve()}?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True)
        rows = connection.execute(
            "SELECT state, COUNT(*) FROM circuit_state GROUP BY state"
        ).fetchall()
        for raw_state, raw_count in rows:
            state = str(raw_state)
            count = int(raw_count)
            if state in counts:
                counts[state] += count
            elif state == "half_open":
                counts["open"] += count
            else:
                counts["unknown"] += count
        return True, counts
    except (OSError, sqlite3.Error, ValueError):
        return False, counts
    finally:
        if connection is not None:
            connection.close()


def build_overview(
    *,
    cache_root: str | Path,
    config: Mapping[str, Any] | None = None,
    provider_ids: Sequence[str] | None = None,
    state_path: str | Path | None = None,
    plugin_version: str = "2.9.1",
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    root = Path(cache_root)
    active_config: Mapping[str, Any] = config if isinstance(config, Mapping) else DEFAULT_CONFIG
    bounded = active_config.get("bounded_context")
    bounded = bounded if isinstance(bounded, Mapping) else {}
    providers = list(provider_ids) if provider_ids is not None else list(PROVIDER_SPECS)
    circuits_available, circuits = _circuit_snapshot(
        Path(state_path) if state_path is not None else root / "state" / "v3.sqlite3"
    )
    receipts = build_receipts(cache_root=root, limit=100)["receipts"]
    benchmarks = build_benchmark_history(cache_root=root, limit=100)["runs"]
    kinds = sorted({run["kind"] for run in benchmarks})
    payload = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "engine": {
            "contract_version": "3.0",
            "plugin_version": plugin_version,
            "state_available": circuits_available,
        },
        "providers": _provider_rows(
            active_config, providers, cache_root=root, now=float(now())
        ),
        "bounds": {
            "max_urls_default": int(bounded.get("max_urls", DEFAULT_MAX_URLS)),
            "max_urls_hard": HARD_MAX_URLS,
            "max_context_chars_default": int(
                bounded.get("max_context_chars", DEFAULT_MAX_CONTEXT_CHARS)
            ),
            "max_context_chars_min": MIN_CONTEXT_CHARS,
            "max_context_chars_hard": MAX_CONTEXT_CHARS,
            "full_text_ttl_seconds": int(
                bounded.get("full_text_ttl_seconds", DEFAULT_FULL_TEXT_TTL_SECONDS)
            ),
            "full_text_max_bytes": int(
                bounded.get("full_text_max_bytes", DEFAULT_FULL_TEXT_MAX_BYTES)
            ),
        },
        "cache": _cache_snapshot(root),
        "circuits": circuits,
        "receipts_summary": {
            "count": len(receipts),
            "latest_timestamp": receipts[0]["timestamp"] if receipts else None,
        },
        "benchmark_summary": {
            "count": len(benchmarks),
            "latest_timestamp": benchmarks[0]["timestamp"] if benchmarks else None,
            "kinds": kinds,
            "extract_collected": "extract" in kinds,
        },
    }
    privacy.assert_operator_payload_safe(payload)
    return payload
