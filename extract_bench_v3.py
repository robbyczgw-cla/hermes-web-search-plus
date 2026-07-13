"""Quota-aware direct-provider extraction benchmark for WS-3.

The runner deliberately calls ``EXTRACT_DISPATCH`` adapters directly. It does
not use provider fallback, retry/cooldown, adaptive stats, response cache, or
the v3 state store. Reports and persisted history contain aggregate metrics
only: target URLs, extracted content, provider error text, and credentials are
never retained.
"""

from __future__ import annotations

import json
import os
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

import fcntl

import operator_privacy_v3 as privacy
import providers as _providers
from config import get_api_key, keyless_public_allowed, provider_configured
from extract import _validate_extract_urls
from http_client import ProviderRequestError
from operator_console_v3 import BENCHMARK_HISTORY_SCHEMA_VERSION, BENCHMARK_OWNER
from provider_adapter_protocol import validate_adapter_result
from provider_dispatch import EXTRACT_DISPATCH
from provider_registry import EXTRACT_PROVIDER_IDS, PROVIDER_SPECS


HISTORY_SCHEMA_VERSION = 1
DEFAULT_TIMEOUT_BUDGET_SECONDS = 120.0
DEFAULT_MAX_RECORDS = 100
DEFAULT_MAX_BYTES = 8 * 1024 * 1024
LATENCY_CEILING_SECONDS = 30.0
CONTENT_TARGET_CHARS_PER_URL = 5_000


def extract_bench_eligible_providers(config: Mapping[str, Any]) -> list[str]:
    """Configured, enabled extraction providers in configured/default order."""
    auto = config.get("auto_routing")
    auto = auto if isinstance(auto, Mapping) else {}
    disabled = set(auto.get("disabled_providers") or [])
    raw_priority = auto.get("extract_provider_priority")
    priority = list(raw_priority) if isinstance(raw_priority, (list, tuple)) else []
    ordered = []
    for provider in [*priority, *EXTRACT_PROVIDER_IDS]:
        if provider in ordered:
            continue
        spec = PROVIDER_SPECS.get(provider)
        if (
            spec is not None
            and spec.supports_extract
            and provider not in disabled
            and provider_configured(provider, dict(config))
        ):
            ordered.append(provider)
    return ordered


def _provider_score(
    *, success_rate: float, latency_seconds: float | None, returned_chars: int, url_count: int
) -> float:
    latency = (
        0.0
        if latency_seconds is None
        else max(0.0, 1.0 - float(latency_seconds) / LATENCY_CEILING_SECONDS)
    )
    content = min(
        1.0,
        float(returned_chars) / max(1, url_count * CONTENT_TARGET_CHARS_PER_URL),
    )
    return round(0.6 * success_rate + 0.2 * latency + 0.2 * content, 3)


def _safe_provider_error_code(exc: Exception) -> str:
    """Classify an exception without retaining its potentially sensitive text."""
    if isinstance(exc, (TimeoutError,)):
        return "timeout"
    if isinstance(exc, ProviderRequestError):
        status = exc.status_code
        if status in {401, 403}:
            return "auth_error"
        if status == 429:
            return "rate_limited"
        if status is not None and status >= 500:
            return "provider_unavailable"
        if exc.transient:
            return "transient_provider_error"
    return "provider_error"


def _bench_one_provider(
    config: Mapping[str, Any],
    *,
    provider: str,
    urls: list[str],
    extract_module: Any,
    monotonic: Callable[[], float],
) -> dict[str, Any]:
    started = monotonic()
    result: Mapping[str, Any] | None = None
    provider_error_code: str | None = None
    try:
        adapter = EXTRACT_DISPATCH[provider]
        key = get_api_key(provider, dict(config))
        result = validate_adapter_result(
            provider,
            "extract",
            adapter(
                extract_module,
                provider,
                urls,
                key,
                "markdown",
                False,
                False,
                False,
                dict(config),
                keyless_public_allowed(provider, dict(config)),
            ),
        )
    except Exception as exc:  # One provider must never abort or leak details from a bench run.
        provider_error_code = _safe_provider_error_code(exc)
    elapsed = round(max(0.0, monotonic() - started), 3)

    results = []
    if isinstance(result, Mapping):
        results = [item for item in (result.get("results") or []) if isinstance(item, Mapping)]

    success_count = 0
    returned_chars = 0
    error_codes: set[str] = set()
    if provider_error_code is not None:
        error_codes.add(provider_error_code)
    else:
        for item in results[: len(urls)]:
            if item.get("error"):
                error_codes.add("url_error")
                continue
            content = item.get("content") or item.get("markdown") or item.get("text") or ""
            if not isinstance(content, str) or not content:
                error_codes.add("empty_content")
                continue
            success_count += 1
            returned_chars += len(content)
        missing_count = max(0, len(urls) - len(results))
        if missing_count:
            error_codes.add("missing_result")

    error_count = (
        len(urls)
        if provider_error_code is not None
        else max(0, len(urls) - success_count)
    )
    success_rate = round(success_count / len(urls), 3) if urls else 0.0
    median_latency = elapsed if success_count else None
    return {
        "provider": provider,
        "score": _provider_score(
            success_rate=success_rate,
            latency_seconds=median_latency,
            returned_chars=returned_chars,
            url_count=len(urls),
        ),
        "url_count": len(urls),
        "success_count": success_count,
        "success_rate": success_rate,
        "median_latency_seconds": median_latency,
        "elapsed_seconds": elapsed,
        "returned_character_count": returned_chars,
        "error_count": error_count,
        "error_codes": sorted(error_codes),
    }


def run_extract_bench(
    config: Mapping[str, Any],
    *,
    urls: Sequence[str],
    providers: Sequence[str] | None = None,
    timeout_budget: float = DEFAULT_TIMEOUT_BUDGET_SECONDS,
    extract_module: Any = _providers,
    monotonic: Callable[[], float] = time.monotonic,
    generated_at: float | None = None,
) -> dict[str, Any]:
    """Benchmark direct extraction adapters and return a secret-free report."""
    validated_urls = _validate_extract_urls(list(urls), dict(config))
    if not validated_urls:
        raise ValueError("at least one extraction URL is required")

    selected: list[str] = []
    skipped: list[dict[str, str]] = []
    requested = list(providers) if providers is not None else extract_bench_eligible_providers(config)
    for provider in requested:
        spec = PROVIDER_SPECS.get(provider)
        if spec is None or not spec.supports_extract or provider not in EXTRACT_DISPATCH:
            skipped.append({"provider": str(provider), "reason": "not_extract_capable"})
        elif not provider_configured(provider, dict(config)):
            skipped.append({"provider": provider, "reason": "not_configured"})
        elif provider not in selected:
            selected.append(provider)

    elapsed_total = 0.0
    rows: list[dict[str, Any]] = []
    for provider in selected:
        if elapsed_total >= float(timeout_budget):
            skipped.append({"provider": provider, "reason": "time_budget_exhausted"})
            continue
        row = _bench_one_provider(
            config,
            provider=provider,
            urls=validated_urls,
            extract_module=extract_module,
            monotonic=monotonic,
        )
        rows.append(row)
        elapsed_total += float(row["elapsed_seconds"])

    current = (config.get("auto_routing") or {}).get("extract_provider_priority") or list(
        EXTRACT_PROVIDER_IDS
    )
    priority_index = {provider: index for index, provider in enumerate(current)}
    rows.sort(
        key=lambda row: (
            -row["score"],
            priority_index.get(row["provider"], len(priority_index)),
            row["provider"],
        )
    )
    report = {
        "schema_version": HISTORY_SCHEMA_VERSION,
        "kind": "extract",
        "generated_at": float(time.time() if generated_at is None else generated_at),
        "ok": any(row["success_count"] for row in rows),
        "url_count": len(validated_urls),
        "providers": rows,
        "skipped_providers": skipped,
        "recommended_priority": [row["provider"] for row in rows],
        "history_note": "Aggregate metrics only; target URLs and extracted content are not retained.",
    }
    return report


def benchmark_history_record(report: Mapping[str, Any]) -> dict[str, Any]:
    """Project a full extract report into the fixed compact Console history DTO."""
    record = {
        "schema_version": HISTORY_SCHEMA_VERSION,
        "kind": "extract",
        "timestamp": float(report["generated_at"]),
        "ok": bool(report.get("ok")),
        "providers": [
            {
                "provider": row["provider"],
                "score": float(row.get("score", 0.0)),
                "success_rate": float(row.get("success_rate", 0.0)),
                "median_latency_seconds": (
                    None
                    if row.get("median_latency_seconds") is None
                    else float(row["median_latency_seconds"])
                ),
                "error_count": int(row.get("error_count", 0)),
            }
            for row in report.get("providers") or []
            if isinstance(row, Mapping) and row.get("provider") in PROVIDER_SPECS
        ],
        "recommended_priority": [
            provider
            for provider in (report.get("recommended_priority") or [])
            if provider in PROVIDER_SPECS
        ],
    }
    privacy.assert_operator_payload_safe(record)
    return record


def _encode_history_record(record: Mapping[str, Any]) -> str:
    privacy.assert_operator_payload_safe(record)
    return json.dumps(
        {
            "owner": BENCHMARK_OWNER,
            "history_schema_version": BENCHMARK_HISTORY_SCHEMA_VERSION,
            "payload": dict(record),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


class BenchmarkHistoryJournal:
    """Marker-owned bounded history writer; best-effort and symlink-refusing."""

    def __init__(
        self,
        cache_root: str | Path,
        *,
        max_records: int = DEFAULT_MAX_RECORDS,
        max_bytes: int = DEFAULT_MAX_BYTES,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.cache_root = Path(os.path.abspath(os.fspath(cache_root)))
        self.path = self.cache_root / "operator" / "v3" / "benchmark-history.jsonl"
        self.max_records = max(0, int(max_records))
        self.max_bytes = max(0, int(max_bytes))
        self.now = now

    @contextmanager
    def _directory(self) -> Iterator[int]:
        flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(os.path.sep, flags)
        try:
            for component in self.path.parent.parts[1:]:
                try:
                    child = os.open(component, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                    child = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            current = os.fstat(descriptor)
            if not stat.S_ISDIR(current.st_mode) or current.st_uid != os.geteuid():
                raise OSError("benchmark history directory is not owned regular storage")
            os.fchmod(descriptor, 0o700)
            yield descriptor
        finally:
            os.close(descriptor)

    @staticmethod
    def _decode(line: str) -> dict[str, Any] | None:
        try:
            envelope = json.loads(line)
        except (TypeError, ValueError):
            return None
        if (
            not isinstance(envelope, dict)
            or envelope.get("owner") != BENCHMARK_OWNER
            or envelope.get("history_schema_version") != BENCHMARK_HISTORY_SCHEMA_VERSION
            or not isinstance(envelope.get("payload"), dict)
        ):
            return None
        payload = dict(envelope["payload"])
        try:
            privacy.assert_operator_payload_safe(payload)
        except ValueError:
            return None
        return payload

    def _read(self, directory: int) -> list[dict[str, Any]] | None:
        try:
            before = os.stat(self.path.name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return []
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid():
            return None
        flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path.name, flags, dir_fd=directory)
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            opened = os.fstat(handle.fileno())
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                return None
            records = []
            for line in handle:
                if not line.strip():
                    continue
                payload = self._decode(line)
                if payload is None:
                    return None
                records.append(payload)
            return records

    def _rewrite(self, directory: int, records: list[dict[str, Any]]) -> None:
        temp_name = f".wsp-v3-benchmark-history-{uuid.uuid4().hex}.tmp"
        content = "".join(f"{_encode_history_record(record)}\n" for record in records)
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(temp_name, flags, 0o600, dir_fd=directory)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path.name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temp_name, dir_fd=directory)
            except FileNotFoundError:
                pass

    def append(self, record: Mapping[str, Any]) -> bool:
        try:
            encoded = _encode_history_record(record)
            with self._directory() as directory:
                lock_flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
                lock_flags |= getattr(os, "O_NOFOLLOW", 0)
                lock = os.open(".benchmark-history.lock", lock_flags, 0o600, dir_fd=directory)
                try:
                    lock_stat = os.fstat(lock)
                    if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_uid != os.geteuid():
                        return False
                    os.fchmod(lock, 0o600)
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    existing = self._read(directory)
                    if existing is None:
                        return False
                    retained = [*existing, dict(record)][-self.max_records :] if self.max_records else []
                    while retained and sum(
                        len(_encode_history_record(item).encode("utf-8")) + 1
                        for item in retained
                    ) > self.max_bytes:
                        retained.pop(0)
                    self._rewrite(directory, retained)
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)
                    os.close(lock)
            return bool(encoded)
        except Exception:
            return False


def format_extract_bench_text(report: Mapping[str, Any]) -> str:
    lines = [
        "Web Search Plus Extract Bench",
        f"OK: {bool(report.get('ok'))}",
        f"Targets per provider: {int(report.get('url_count', 0))}",
        "",
        "Providers (best first):",
    ]
    for row in report.get("providers") or []:
        median = row.get("median_latency_seconds")
        latency = "-" if median is None else f"{float(median):.2f}s"
        lines.append(
            "  {provider:<12} score={score:.3f} success={success}/{total} "
            "median={latency} chars={chars} errors={errors}".format(
                provider=row["provider"],
                score=float(row.get("score", 0.0)),
                success=int(row.get("success_count", 0)),
                total=int(row.get("url_count", 0)),
                latency=latency,
                chars=int(row.get("returned_character_count", 0)),
                errors=int(row.get("error_count", 0)),
            )
        )
    for skipped in report.get("skipped_providers") or []:
        lines.append(f"  - {skipped.get('provider')}: skipped ({skipped.get('reason')})")
    lines.extend(
        [
            "",
            "Recommended auto_routing.extract_provider_priority:",
            "  " + ", ".join(report.get("recommended_priority") or []),
            "Aggregate metrics only; target URLs and extracted content were not retained.",
        ]
    )
    return "\n".join(lines)
