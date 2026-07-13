#!/usr/bin/env python3
"""Dry-run-first migration of legacy WSP JSON state into SQLite v3.

The migration is deliberately reversible and compatibility-safe: both legacy
JSON files are imported in one SQLite transaction, but neither source file is
modified or deleted. Raw legacy error strings are never persisted in SQLite.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from state_store_v3 import SQLiteStateStore


BACKUP_OWNER = "web-search-plus-state-migration-v3"
BACKUP_SCHEMA_VERSION = 1
MIGRATION_ID = "legacy-json-v1"
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_PROVIDERS = 256
MAX_SAMPLES_PER_PROVIDER = 1000
_PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_BACKUP_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[a-f0-9]{12}(?:-[0-9]+)?$")


@dataclass(frozen=True)
class MigrationReport:
    action: str
    status: str
    dry_run: bool
    sqlite_available: bool
    health_providers: int = 0
    adaptive_providers: int = 0
    adaptive_samples: int = 0
    source_digest: str | None = None
    backup_id: str | None = None
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


def render_migration_report(report: MigrationReport) -> str:
    """Render a deterministic, support-safe report with no local paths."""
    return json.dumps(report.to_dict(), sort_keys=True, separators=(",", ":"))


def _safe_regular_file(path: Path) -> bool:
    if path.is_symlink():
        raise ValueError("symlink_source")
    if path.exists() and not path.is_file():
        raise ValueError("non_regular_source")
    return path.exists()


def _read_json_mapping(path: Path) -> dict[str, Any]:
    if not _safe_regular_file(path):
        return {}
    if path.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError("source_too_large")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_json") from exc
    if not isinstance(value, dict) or len(value) > MAX_PROVIDERS:
        raise ValueError("invalid_mapping")
    return value


def _provider_name(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid_provider")
    normalized = value.strip().lower()
    if not _PROVIDER_RE.fullmatch(normalized):
        raise ValueError("invalid_provider")
    return normalized


def _nonnegative_int(value: Any, *, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid_number")
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0:
        raise ValueError("invalid_number")
    return int(numeric)


def _normalize_health(raw: dict[str, Any]) -> list[tuple[str, int, int, int, int]]:
    rows: list[tuple[str, int, int, int, int]] = []
    for provider_value, entry in raw.items():
        provider = _provider_name(provider_value)
        if not isinstance(entry, dict):
            raise ValueError("invalid_health_entry")
        rows.append(
            (
                provider,
                _nonnegative_int(entry.get("failure_count")),
                _nonnegative_int(entry.get("cooldown_until")),
                _nonnegative_int(entry.get("cooldown_seconds")),
                _nonnegative_int(entry.get("last_failure_at")),
            )
        )
    return sorted(rows)


def _normalize_samples(
    raw: dict[str, Any],
) -> list[tuple[str, int, int, int, int, int]]:
    rows: list[tuple[str, int, int, int, int, int]] = []
    for provider_value, samples in raw.items():
        provider = _provider_name(provider_value)
        if not isinstance(samples, list) or len(samples) > MAX_SAMPLES_PER_PROVIDER:
            raise ValueError("invalid_sample_list")
        for source_index, sample in enumerate(samples):
            if not isinstance(sample, dict):
                raise ValueError("invalid_sample")
            latency = sample.get("lat", 0)
            if isinstance(latency, bool) or not isinstance(latency, (int, float)):
                raise ValueError("invalid_latency")
            latency_value = float(latency)
            if not math.isfinite(latency_value) or latency_value < 0:
                raise ValueError("invalid_latency")
            error = sample.get("err", False)
            if not isinstance(error, bool):
                raise ValueError("invalid_error_flag")
            rows.append(
                (
                    provider,
                    source_index,
                    _nonnegative_int(sample.get("t")),
                    int(round(latency_value * 1000)),
                    _nonnegative_int(sample.get("n")),
                    int(error),
                )
            )
    return sorted(rows)


def _source_digest(
    health_rows: list[tuple[str, int, int, int, int]],
    sample_rows: list[tuple[str, int, int, int, int, int]],
) -> str:
    canonical = json.dumps(
        {"health": health_rows, "adaptive_samples": sample_rows},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _nearest_existing_parent(path: Path) -> Path:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def _sqlite_preflight(path: Path) -> tuple[bool, str | None]:
    try:
        if path.is_symlink():
            return False, None
        if not path.exists():
            parent = _nearest_existing_parent(path.parent)
            return parent.is_dir() and os.access(parent, os.W_OK), None
        if not path.is_file():
            return False, None
        uri = f"file:{path.resolve()}?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True)
        try:
            quick_check = connection.execute("PRAGMA quick_check").fetchone()
            if not quick_check or quick_check[0] != "ok":
                return False, None
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                ("legacy_state_migrations",),
            ).fetchone()
            if not exists:
                return True, None
            row = connection.execute(
                "SELECT source_digest FROM legacy_state_migrations WHERE migration_id=?",
                (MIGRATION_ID,),
            ).fetchone()
            return True, str(row[0]) if row else None
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return False, None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_file_hashes(cache_root: Path) -> dict[str, str | None]:
    hashes: dict[str, str | None] = {}
    for name in ("provider_health.json", "provider_stats.json"):
        source = cache_root / name
        hashes[name] = _sha256_file(source) if _safe_regular_file(source) else None
    return hashes


def _source_files_match(
    cache_root: Path, expected: dict[str, str | None]
) -> bool:
    try:
        return _source_file_hashes(cache_root) == expected
    except (OSError, ValueError):
        return False


def _copy_secure(source: Path, destination: Path) -> None:
    _safe_regular_file(source)
    shutil.copyfile(source, destination)
    os.chmod(destination, 0o600)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _next_backup_id(backup_root: Path, now: int, digest: str) -> str:
    timestamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = f"{timestamp}-{digest[:12]}"
    candidate = base
    counter = 1
    while (backup_root / candidate).exists():
        candidate = f"{base}-{counter}"
        counter += 1
    return candidate


def _create_backup(
    *,
    cache_root: Path,
    state_path: Path,
    backup_root: Path,
    now: int,
    source_digest: str,
    health_providers: int,
    adaptive_providers: int,
    adaptive_samples: int,
    expected_source_hashes: dict[str, str | None],
) -> str:
    secret_path = state_path.with_name(f"{state_path.name}.secret")
    if state_path.is_symlink() or secret_path.is_symlink():
        raise OSError("unsafe_state_path")
    if not _source_files_match(cache_root, expected_source_hashes):
        raise OSError("legacy_source_changed")
    if backup_root.is_symlink():
        raise OSError("unsafe_backup_root")
    backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(backup_root, 0o700)
    backup_id = _next_backup_id(backup_root, now, source_digest)
    backup_dir = backup_root / backup_id
    backup_dir.mkdir(mode=0o700)
    files: dict[str, str] = {}
    source_paths = {
        "provider_health.json": cache_root / "provider_health.json",
        "provider_stats.json": cache_root / "provider_stats.json",
        "state.sqlite3.secret": secret_path,
    }
    manifest = {
        "owner": BACKUP_OWNER,
        "schema_version": BACKUP_SCHEMA_VERSION,
        "backup_id": backup_id,
        "created_at": now,
        "source_digest": source_digest,
        "database_existed": state_path.exists(),
        "secret_existed": secret_path.exists(),
        "health_existed": source_paths["provider_health.json"].exists(),
        "stats_existed": source_paths["provider_stats.json"].exists(),
        "health_providers": health_providers,
        "adaptive_providers": adaptive_providers,
        "adaptive_samples": adaptive_samples,
        "files": files,
    }
    try:
        if state_path.exists():
            _safe_regular_file(state_path)
            target = backup_dir / "state.sqlite3"
            source_connection = sqlite3.connect(
                f"file:{state_path.resolve()}?mode=ro", uri=True
            )
            destination_connection = sqlite3.connect(target)
            try:
                source_connection.backup(destination_connection)
            finally:
                destination_connection.close()
                source_connection.close()
            os.chmod(target, 0o600)
            files[target.name] = _sha256_file(target)
        for name, source in source_paths.items():
            if source.exists():
                target = backup_dir / name
                _copy_secure(source, target)
                files[name] = _sha256_file(target)
        for name, expected_digest in expected_source_hashes.items():
            if files.get(name) != expected_digest:
                raise OSError("legacy_source_changed")
        if not _source_files_match(cache_root, expected_source_hashes):
            raise OSError("legacy_source_changed")
        _atomic_json(backup_dir / "manifest.json", manifest)
    except Exception:
        shutil.rmtree(backup_dir, ignore_errors=True)
        if backup_root.exists() and not any(backup_root.iterdir()):
            backup_root.rmdir()
        raise
    return backup_id


def _report(
    *,
    action: str,
    status: str,
    dry_run: bool,
    sqlite_available: bool,
    health_rows: list[tuple[str, int, int, int, int]] | None = None,
    sample_rows: list[tuple[str, int, int, int, int, int]] | None = None,
    source_digest: str | None = None,
    backup_id: str | None = None,
    error_code: str | None = None,
) -> MigrationReport:
    health_rows = health_rows or []
    sample_rows = sample_rows or []
    return MigrationReport(
        action=action,
        status=status,
        dry_run=dry_run,
        sqlite_available=sqlite_available,
        health_providers=len(health_rows),
        adaptive_providers=len({row[0] for row in sample_rows}),
        adaptive_samples=len(sample_rows),
        source_digest=source_digest,
        backup_id=backup_id,
        error_code=error_code,
    )


def migrate_legacy_state(
    *,
    cache_root: str | Path,
    state_path: str | Path,
    backup_root: str | Path | None = None,
    dry_run: bool = True,
    now: int | None = None,
) -> MigrationReport:
    """Plan or atomically apply the two-file legacy-state migration."""
    cache_root = Path(cache_root)
    state_path = Path(state_path)
    backup_root = (
        Path(backup_root)
        if backup_root is not None
        else state_path.parent / "migration-backups"
    )
    action = "dry_run" if dry_run else "apply"
    try:
        source_file_hashes = _source_file_hashes(cache_root)
        health_raw = _read_json_mapping(cache_root / "provider_health.json")
        stats_raw = _read_json_mapping(cache_root / "provider_stats.json")
        if _source_file_hashes(cache_root) != source_file_hashes:
            raise ValueError("legacy_source_changed")
        health_rows = _normalize_health(health_raw)
        sample_rows = _normalize_samples(stats_raw)
    except (OSError, ValueError):
        return _report(
            action=action,
            status="blocked",
            dry_run=dry_run,
            sqlite_available=False,
            error_code="invalid_legacy_state",
        )
    digest = _source_digest(health_rows, sample_rows)
    sqlite_available, current_digest = _sqlite_preflight(state_path)
    if not sqlite_available:
        return _report(
            action=action,
            status="degraded",
            dry_run=dry_run,
            sqlite_available=False,
            health_rows=health_rows,
            sample_rows=sample_rows,
            source_digest=digest,
            error_code="sqlite_unavailable",
        )
    if not health_raw and not stats_raw:
        return _report(
            action=action,
            status="unchanged",
            dry_run=dry_run,
            sqlite_available=True,
            source_digest=digest,
        )
    if current_digest == digest:
        return _report(
            action=action,
            status="unchanged",
            dry_run=dry_run,
            sqlite_available=True,
            health_rows=health_rows,
            sample_rows=sample_rows,
            source_digest=digest,
        )
    if dry_run:
        return _report(
            action=action,
            status="ready",
            dry_run=True,
            sqlite_available=True,
            health_rows=health_rows,
            sample_rows=sample_rows,
            source_digest=digest,
        )

    applied_at = int(now if now is not None else time.time())
    try:
        backup_id = _create_backup(
            cache_root=cache_root,
            state_path=state_path,
            backup_root=backup_root,
            now=applied_at,
            source_digest=digest,
            health_providers=len(health_rows),
            adaptive_providers=len({row[0] for row in sample_rows}),
            adaptive_samples=len(sample_rows),
            expected_source_hashes=source_file_hashes,
        )
    except (OSError, sqlite3.Error, ValueError):
        return _report(
            action=action,
            status="blocked",
            dry_run=False,
            sqlite_available=True,
            health_rows=health_rows,
            sample_rows=sample_rows,
            source_digest=digest,
            error_code="backup_failed",
        )

    if not _source_files_match(cache_root, source_file_hashes):
        return _report(
            action=action,
            status="blocked",
            dry_run=False,
            sqlite_available=True,
            health_rows=health_rows,
            sample_rows=sample_rows,
            source_digest=digest,
            backup_id=backup_id,
            error_code="legacy_source_changed",
        )

    try:
        store = SQLiteStateStore(state_path)
        if not store.available:
            raise sqlite3.OperationalError("state_store_unavailable")
        connection = sqlite3.connect(state_path, timeout=5, isolation_level=None)
        try:
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM legacy_provider_health")
            connection.execute("DELETE FROM adaptive_samples_v3")
            connection.executemany(
                """
                INSERT INTO legacy_provider_health (
                    provider, failure_count, cooldown_until, cooldown_seconds,
                    last_failure_at, source_digest, migrated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [(*row, digest, applied_at) for row in health_rows],
            )
            connection.executemany(
                """
                INSERT INTO adaptive_samples_v3 (
                    provider, source_index, sample_time, latency_ms,
                    result_count, error, source_digest, migrated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [(*row, digest, applied_at) for row in sample_rows],
            )
            connection.execute(
                """
                INSERT INTO legacy_state_migrations (
                    migration_id, source_digest, applied_at, health_providers,
                    adaptive_providers, adaptive_samples
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(migration_id) DO UPDATE SET
                    source_digest=excluded.source_digest,
                    applied_at=excluded.applied_at,
                    health_providers=excluded.health_providers,
                    adaptive_providers=excluded.adaptive_providers,
                    adaptive_samples=excluded.adaptive_samples
                """,
                (
                    MIGRATION_ID,
                    digest,
                    applied_at,
                    len(health_rows),
                    len({row[0] for row in sample_rows}),
                    len(sample_rows),
                ),
            )
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
        os.chmod(state_path, 0o600)
        secret_path = state_path.with_name(f"{state_path.name}.secret")
        if secret_path.exists():
            os.chmod(secret_path, 0o600)
    except (OSError, sqlite3.Error):
        rollback = rollback_legacy_state(
            state_path=state_path,
            backup_root=backup_root,
            backup_id=backup_id,
        )
        return _report(
            action=action,
            status="degraded" if rollback.status == "rolled_back" else "blocked",
            dry_run=False,
            sqlite_available=False,
            health_rows=health_rows,
            sample_rows=sample_rows,
            source_digest=digest,
            backup_id=backup_id,
            error_code=(
                "sqlite_write_failed" if rollback.status == "rolled_back" else "rollback_failed"
            ),
        )

    return _report(
        action=action,
        status="applied",
        dry_run=False,
        sqlite_available=True,
        health_rows=health_rows,
        sample_rows=sample_rows,
        source_digest=digest,
        backup_id=backup_id,
    )


def _validated_manifest(backup_dir: Path, backup_id: str) -> dict[str, Any]:
    if backup_dir.is_symlink() or not backup_dir.is_dir():
        raise ValueError("invalid_backup")
    manifest_path = backup_dir / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("invalid_backup")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        not isinstance(manifest, dict)
        or manifest.get("owner") != BACKUP_OWNER
        or manifest.get("schema_version") != BACKUP_SCHEMA_VERSION
        or manifest.get("backup_id") != backup_id
        or not isinstance(manifest.get("files"), dict)
    ):
        raise ValueError("invalid_backup")
    boolean_fields = (
        "database_existed",
        "secret_existed",
        "health_existed",
        "stats_existed",
    )
    count_fields = ("health_providers", "adaptive_providers", "adaptive_samples")
    if any(not isinstance(manifest.get(field), bool) for field in boolean_fields):
        raise ValueError("invalid_backup")
    if any(
        isinstance(manifest.get(field), bool)
        or not isinstance(manifest.get(field), int)
        or manifest[field] < 0
        for field in count_fields
    ):
        raise ValueError("invalid_backup")
    if not isinstance(manifest.get("created_at"), int) or not re.fullmatch(
        r"[a-f0-9]{64}", str(manifest.get("source_digest", ""))
    ):
        raise ValueError("invalid_backup")
    expected_files = {
        name
        for name, flag in (
            ("state.sqlite3", manifest["database_existed"]),
            ("state.sqlite3.secret", manifest["secret_existed"]),
            ("provider_health.json", manifest["health_existed"]),
            ("provider_stats.json", manifest["stats_existed"]),
        )
        if flag
    }
    if set(manifest["files"]) != expected_files:
        raise ValueError("invalid_backup")
    for name, expected_digest in manifest["files"].items():
        if name not in {
            "state.sqlite3",
            "state.sqlite3.secret",
            "provider_health.json",
            "provider_stats.json",
        }:
            raise ValueError("invalid_backup")
        candidate = backup_dir / name
        if candidate.is_symlink() or not candidate.is_file():
            raise ValueError("invalid_backup")
        if not isinstance(expected_digest, str) or _sha256_file(candidate) != expected_digest:
            raise ValueError("invalid_backup")
    return manifest


def _atomic_restore(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".restore", dir=str(destination.parent)
    )
    os.close(descriptor)
    try:
        shutil.copyfile(source, temporary)
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def rollback_legacy_state(
    *,
    state_path: str | Path,
    backup_root: str | Path,
    backup_id: str,
) -> MigrationReport:
    """Restore the exact pre-migration database/secret from a verified backup."""
    state_path = Path(state_path)
    backup_root = Path(backup_root)
    secret_path = state_path.with_name(f"{state_path.name}.secret")
    live_paths = (
        state_path,
        secret_path,
        Path(f"{state_path}-wal"),
        Path(f"{state_path}-shm"),
    )
    if (
        not _BACKUP_ID_RE.fullmatch(backup_id)
        or backup_root.is_symlink()
        or any(path.is_symlink() for path in live_paths)
    ):
        return _report(
            action="rollback",
            status="blocked",
            dry_run=False,
            sqlite_available=False,
            backup_id=backup_id or None,
            error_code="backup_invalid",
        )
    backup_dir = backup_root / backup_id
    try:
        manifest = _validated_manifest(backup_dir, backup_id)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{state_path}{suffix}")
            if sidecar.exists() and not sidecar.is_symlink():
                sidecar.unlink()
        if manifest.get("database_existed"):
            _atomic_restore(backup_dir / "state.sqlite3", state_path)
        elif state_path.exists() and not state_path.is_symlink():
            state_path.unlink()
        if manifest.get("secret_existed"):
            _atomic_restore(backup_dir / "state.sqlite3.secret", secret_path)
        elif secret_path.exists() and not secret_path.is_symlink():
            secret_path.unlink()
    except (OSError, ValueError, json.JSONDecodeError):
        return _report(
            action="rollback",
            status="blocked",
            dry_run=False,
            sqlite_available=False,
            backup_id=backup_id,
            error_code="backup_invalid",
        )
    return MigrationReport(
        action="rollback",
        status="rolled_back",
        dry_run=False,
        sqlite_available=bool(manifest.get("database_existed")),
        health_providers=int(manifest.get("health_providers", 0)),
        adaptive_providers=int(manifest.get("adaptive_providers", 0)),
        adaptive_samples=int(manifest.get("adaptive_samples", 0)),
        source_digest=str(manifest.get("source_digest", "")),
        backup_id=backup_id,
    )


def _default_cache_root() -> Path:
    configured = os.environ.get("WSP_CACHE_DIR")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parent.parent / ".cache"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Dry-run-first migration of WSP legacy JSON state into SQLite v3"
    )
    parser.add_argument("--cache-root", type=Path, default=_default_cache_root())
    parser.add_argument("--state-path", type=Path)
    parser.add_argument("--backup-root", type=Path)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true", help="Apply after backup")
    action.add_argument("--rollback", metavar="BACKUP_ID", help="Restore a verified backup")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cache_root = args.cache_root
    state_path = args.state_path or cache_root / "v3" / "state.sqlite3"
    backup_root = args.backup_root or state_path.parent / "migration-backups"
    if args.rollback:
        report = rollback_legacy_state(
            state_path=state_path,
            backup_root=backup_root,
            backup_id=args.rollback,
        )
    else:
        report = migrate_legacy_state(
            cache_root=cache_root,
            state_path=state_path,
            backup_root=backup_root,
            dry_run=not args.apply,
        )
    print(render_migration_report(report))
    return 0 if report.status in {"ready", "applied", "unchanged", "rolled_back"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
