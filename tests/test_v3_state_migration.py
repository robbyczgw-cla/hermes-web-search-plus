from __future__ import annotations

import hashlib
import json
import sqlite3
import stat

import state_migration_v3
from state_migration_v3 import (
    BACKUP_OWNER,
    migrate_legacy_state,
    main as migration_main,
    render_migration_report,
    rollback_legacy_state,
)
from state_store_v3 import SCHEMA_VERSION, SQLiteStateStore


def _sha256(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_sources(cache_root) -> tuple:
    cache_root.mkdir(parents=True, exist_ok=True)
    health = cache_root / "provider_health.json"
    stats = cache_root / "provider_stats.json"
    health.write_text(
        json.dumps(
            {
                "serper": {
                    "failure_count": 2,
                    "cooldown_until": 1700000120,
                    "cooldown_seconds": 300,
                    "last_failure_at": 1700000000,
                    "last_error": "Authorization: Bearer private-token-must-not-migrate",
                },
                "linkup": {
                    "failure_count": 1,
                    "cooldown_until": 1700000060,
                    "cooldown_seconds": 60,
                    "last_failure_at": 1699999999,
                },
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    stats.write_text(
        json.dumps(
            {
                "serper": [
                    {"t": 1700000000, "lat": 0.25, "n": 8, "err": False},
                    {"t": 1700000000, "lat": 0.25, "n": 8, "err": False},
                ],
                "linkup": [
                    {"t": 1700000001, "lat": 1.5, "n": 0, "err": True}
                ],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return health, stats


def _logical_dump(path) -> str:
    with sqlite3.connect(path) as connection:
        return "\n".join(connection.iterdump())


def test_schema_v2_contains_legacy_health_and_adaptive_tables(tmp_path):
    path = tmp_path / "state.sqlite3"
    SQLiteStateStore(path)

    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        version = connection.execute("PRAGMA user_version").fetchone()[0]

    assert SCHEMA_VERSION == 2
    assert version == 2
    assert {
        "circuit_state",
        "budget_ledger",
        "legacy_provider_health",
        "adaptive_samples_v3",
        "legacy_state_migrations",
    }.issubset(tables)


def test_dry_run_is_default_and_changes_no_bytes(tmp_path):
    cache_root = tmp_path / "cache"
    health, stats = _write_sources(cache_root)
    state_path = cache_root / "v3" / "state.sqlite3"
    SQLiteStateStore(state_path)
    before = {
        "health": _sha256(health),
        "stats": _sha256(stats),
        "state": _sha256(state_path),
        "secret": _sha256(state_path.with_name("state.sqlite3.secret")),
    }

    report = migrate_legacy_state(
        cache_root=cache_root,
        state_path=state_path,
        backup_root=tmp_path / "backups",
        now=1700000200,
    )

    assert report.action == "dry_run"
    assert report.status == "ready"
    assert report.dry_run is True
    assert report.health_providers == 2
    assert report.adaptive_providers == 2
    assert report.adaptive_samples == 3
    assert report.sqlite_available is True
    assert report.backup_id is None
    assert not (tmp_path / "backups").exists()
    assert before == {
        "health": _sha256(health),
        "stats": _sha256(stats),
        "state": _sha256(state_path),
        "secret": _sha256(state_path.with_name("state.sqlite3.secret")),
    }
    rendered = render_migration_report(report)
    assert "private-token-must-not-migrate" not in rendered
    assert str(tmp_path) not in rendered


def test_apply_imports_both_sources_atomically_and_second_run_is_noop(tmp_path):
    cache_root = tmp_path / "cache"
    health, stats = _write_sources(cache_root)
    state_path = cache_root / "v3" / "state.sqlite3"
    SQLiteStateStore(state_path)
    source_hashes = (_sha256(health), _sha256(stats))
    backup_root = tmp_path / "backups"

    first = migrate_legacy_state(
        cache_root=cache_root,
        state_path=state_path,
        backup_root=backup_root,
        dry_run=False,
        now=1700000200,
    )

    assert first.status == "applied"
    assert first.action == "apply"
    assert first.backup_id
    backup_dir = backup_root / first.backup_id
    manifest = json.loads((backup_dir / "manifest.json").read_text())
    assert stat.S_IMODE(backup_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(backup_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((backup_dir / "manifest.json").stat().st_mode) == 0o600
    assert manifest["owner"] == BACKUP_OWNER
    assert (backup_dir / "state.sqlite3").exists()
    assert (backup_dir / "state.sqlite3.secret").exists()
    assert (backup_dir / "provider_health.json").exists()
    assert (backup_dir / "provider_stats.json").exists()
    with sqlite3.connect(backup_dir / "state.sqlite3") as backup_connection:
        assert backup_connection.execute(
            "SELECT COUNT(*) FROM legacy_provider_health"
        ).fetchone()[0] == 0
        assert backup_connection.execute(
            "SELECT COUNT(*) FROM adaptive_samples_v3"
        ).fetchone()[0] == 0

    with sqlite3.connect(state_path) as connection:
        health_rows = connection.execute(
            """
            SELECT provider, failure_count, cooldown_until, cooldown_seconds,
                   last_failure_at
            FROM legacy_provider_health ORDER BY provider
            """
        ).fetchall()
        samples = connection.execute(
            """
            SELECT provider, source_index, sample_time, latency_ms,
                   result_count, error
            FROM adaptive_samples_v3 ORDER BY provider, source_index
            """
        ).fetchall()
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(legacy_provider_health)")
        }
        digest = connection.execute(
            "SELECT source_digest FROM legacy_state_migrations WHERE migration_id=?",
            ("legacy-json-v1",),
        ).fetchone()[0]

    assert health_rows == [
        ("linkup", 1, 1700000060, 60, 1699999999),
        ("serper", 2, 1700000120, 300, 1700000000),
    ]
    assert samples == [
        ("linkup", 0, 1700000001, 1500, 0, 1),
        ("serper", 0, 1700000000, 250, 8, 0),
        ("serper", 1, 1700000000, 250, 8, 0),
    ]
    assert "last_error" not in columns
    assert digest == first.source_digest
    assert source_hashes == (_sha256(health), _sha256(stats))

    before_second = (_sha256(state_path), _logical_dump(state_path))
    second = migrate_legacy_state(
        cache_root=cache_root,
        state_path=state_path,
        backup_root=backup_root,
        dry_run=False,
        now=1700000300,
    )

    assert second.status == "unchanged"
    assert second.backup_id is None
    assert (_sha256(state_path), _logical_dump(state_path)) == before_second
    assert [path.name for path in backup_root.iterdir()] == [first.backup_id]


def test_rollback_restores_pre_migration_database_and_keeps_sources(tmp_path):
    cache_root = tmp_path / "cache"
    health, stats = _write_sources(cache_root)
    state_path = cache_root / "v3" / "state.sqlite3"
    store = SQLiteStateStore(state_path)
    store.configure_budget("existing", "window", limit_units=7)
    before_dump = _logical_dump(state_path)
    source_hashes = (_sha256(health), _sha256(stats))
    backup_root = tmp_path / "backups"

    applied = migrate_legacy_state(
        cache_root=cache_root,
        state_path=state_path,
        backup_root=backup_root,
        dry_run=False,
        now=1700000200,
    )
    assert _logical_dump(state_path) != before_dump

    rolled_back = rollback_legacy_state(
        state_path=state_path,
        backup_root=backup_root,
        backup_id=applied.backup_id or "",
    )

    assert rolled_back.status == "rolled_back"
    assert rolled_back.action == "rollback"
    assert _logical_dump(state_path) == before_dump
    assert source_hashes == (_sha256(health), _sha256(stats))


def test_rollback_removes_database_created_by_migration(tmp_path):
    cache_root = tmp_path / "cache"
    _write_sources(cache_root)
    state_path = cache_root / "v3" / "state.sqlite3"
    backup_root = tmp_path / "backups"

    applied = migrate_legacy_state(
        cache_root=cache_root,
        state_path=state_path,
        backup_root=backup_root,
        dry_run=False,
        now=1700000200,
    )
    assert state_path.exists()
    assert state_path.with_name("state.sqlite3.secret").exists()

    rolled_back = rollback_legacy_state(
        state_path=state_path,
        backup_root=backup_root,
        backup_id=applied.backup_id or "",
    )

    assert rolled_back.status == "rolled_back"
    assert not state_path.exists()
    assert not state_path.with_name("state.sqlite3.secret").exists()


def test_corrupt_sqlite_degrades_without_touching_any_file(tmp_path):
    cache_root = tmp_path / "cache"
    health, stats = _write_sources(cache_root)
    state_path = cache_root / "v3" / "state.sqlite3"
    state_path.parent.mkdir(parents=True)
    state_path.write_bytes(b"not sqlite")
    before = (_sha256(health), _sha256(stats), _sha256(state_path))

    report = migrate_legacy_state(
        cache_root=cache_root,
        state_path=state_path,
        backup_root=tmp_path / "backups",
        dry_run=False,
        now=1700000200,
    )

    assert report.status == "degraded"
    assert report.error_code == "sqlite_unavailable"
    assert report.sqlite_available is False
    assert report.backup_id is None
    assert before == (_sha256(health), _sha256(stats), _sha256(state_path))
    assert not state_path.with_name("state.sqlite3.secret").exists()
    assert not (tmp_path / "backups").exists()


def test_invalid_source_blocks_without_creating_database(tmp_path):
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    (cache_root / "provider_health.json").write_text("{broken", encoding="utf-8")
    (cache_root / "provider_stats.json").write_text("{}\n", encoding="utf-8")
    state_path = cache_root / "v3" / "state.sqlite3"

    report = migrate_legacy_state(
        cache_root=cache_root,
        state_path=state_path,
        backup_root=tmp_path / "backups",
        dry_run=False,
    )

    assert report.status == "blocked"
    assert report.error_code == "invalid_legacy_state"
    assert not state_path.exists()
    assert not (tmp_path / "backups").exists()


def test_write_failure_rolls_back_both_import_tables(tmp_path):
    cache_root = tmp_path / "cache"
    _write_sources(cache_root)
    state_path = cache_root / "v3" / "state.sqlite3"
    SQLiteStateStore(state_path)
    with sqlite3.connect(state_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_adaptive_import
            BEFORE INSERT ON adaptive_samples_v3
            BEGIN
                SELECT RAISE(ABORT, 'forced failure');
            END
            """
        )
    before_dump = _logical_dump(state_path)

    report = migrate_legacy_state(
        cache_root=cache_root,
        state_path=state_path,
        backup_root=tmp_path / "backups",
        dry_run=False,
        now=1700000200,
    )

    assert report.status == "degraded"
    assert report.error_code == "sqlite_write_failed"
    assert _logical_dump(state_path) == before_dump


def test_tampered_backup_is_rejected_without_touching_live_state(tmp_path):
    cache_root = tmp_path / "cache"
    _write_sources(cache_root)
    state_path = cache_root / "v3" / "state.sqlite3"
    backup_root = tmp_path / "backups"
    applied = migrate_legacy_state(
        cache_root=cache_root,
        state_path=state_path,
        backup_root=backup_root,
        dry_run=False,
        now=1700000200,
    )
    before = _logical_dump(state_path)
    backup_file = backup_root / (applied.backup_id or "") / "provider_health.json"
    backup_file.write_bytes(backup_file.read_bytes() + b"tamper")

    report = rollback_legacy_state(
        state_path=state_path,
        backup_root=backup_root,
        backup_id=applied.backup_id or "",
    )

    assert report.status == "blocked"
    assert report.error_code == "backup_invalid"
    assert _logical_dump(state_path) == before


def test_source_change_after_backup_blocks_before_sqlite_write(
    tmp_path, monkeypatch
):
    cache_root = tmp_path / "cache"
    health, _stats = _write_sources(cache_root)
    state_path = cache_root / "v3" / "state.sqlite3"
    SQLiteStateStore(state_path)
    before_dump = _logical_dump(state_path)
    original_create_backup = state_migration_v3._create_backup

    def create_then_mutate(**kwargs):
        backup_id = original_create_backup(**kwargs)
        health.write_text("{}\n", encoding="utf-8")
        return backup_id

    monkeypatch.setattr(state_migration_v3, "_create_backup", create_then_mutate)
    report = migrate_legacy_state(
        cache_root=cache_root,
        state_path=state_path,
        backup_root=tmp_path / "backups",
        dry_run=False,
        now=1700000200,
    )

    assert report.status == "blocked"
    assert report.error_code == "legacy_source_changed"
    assert report.backup_id
    assert _logical_dump(state_path) == before_dump


def test_rollback_rejects_live_state_symlink(tmp_path):
    cache_root = tmp_path / "cache"
    _write_sources(cache_root)
    state_path = cache_root / "v3" / "state.sqlite3"
    SQLiteStateStore(state_path)
    backup_root = tmp_path / "backups"
    applied = migrate_legacy_state(
        cache_root=cache_root,
        state_path=state_path,
        backup_root=backup_root,
        dry_run=False,
        now=1700000200,
    )
    outside = tmp_path / "outside.sqlite3"
    outside.write_bytes(b"outside-must-not-change")
    state_path.unlink()
    state_path.symlink_to(outside)

    report = rollback_legacy_state(
        state_path=state_path,
        backup_root=backup_root,
        backup_id=applied.backup_id or "",
    )

    assert report.status == "blocked"
    assert report.error_code == "backup_invalid"
    assert state_path.is_symlink()
    assert outside.read_bytes() == b"outside-must-not-change"


def test_cli_defaults_to_path_free_dry_run_json(tmp_path, capsys):
    cache_root = tmp_path / "cache"
    _write_sources(cache_root)

    exit_code = migration_main(["--cache-root", str(cache_root)])

    assert exit_code == 0
    output = capsys.readouterr().out.strip()
    report = json.loads(output)
    assert report["action"] == "dry_run"
    assert report["status"] == "ready"
    assert report["dry_run"] is True
    assert str(tmp_path) not in output


def test_search_cli_exposes_state_migrate_as_dry_run_default(tmp_path, monkeypatch, capsys):
    import search

    cache_root = tmp_path / "cache"
    _write_sources(cache_root)
    parser = search.build_parser({})
    parsed = parser.parse_args(["state-migrate"])
    assert parsed.command == "state-migrate"
    assert parsed.apply is False
    assert parsed.rollback is None

    monkeypatch.setattr(search, "CACHE_DIR", cache_root)
    monkeypatch.setattr(search, "load_config", lambda: {})
    monkeypatch.setattr(search.sys, "argv", ["search.py", "state-migrate"])
    search.main()

    output = capsys.readouterr().out.strip()
    report = json.loads(output)
    assert report["action"] == "dry_run"
    assert report["status"] == "ready"
    assert not (cache_root / "v3" / "state.sqlite3").exists()
