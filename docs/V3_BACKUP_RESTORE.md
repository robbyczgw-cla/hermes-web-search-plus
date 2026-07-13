# WSP 3.0 Backup and Restore

## Migration backups

`python3 search.py state-migrate --apply` writes backups under:

```text
CACHE_DIR/v3/migration-backups/BACKUP_ID/
```

Use `--migration-backup-root PATH` only when the default location is unsuitable. The same option must be supplied to rollback.

Each backup is marker-owned and contains a manifest plus the files that existed before migration:

- `state.sqlite3`
- `state.sqlite3.secret`
- `provider_health.json`
- `provider_stats.json`

Directories use mode `0700`; copied files and the manifest use `0600`. The manifest records SHA-256 digests, source digest, schema version, row counts, and whether the database and secret existed before apply.

## What apply changes

Apply changes only the v3 SQLite database and its local secret. The two legacy JSON source files are copied into the backup for evidence but remain untouched in the live cache.

Both legacy sources are imported in one SQLite transaction. A write failure triggers automatic verified rollback.

## Verified rollback

```bash
python3 search.py state-migrate --rollback BACKUP_ID
```

Rollback fails closed when:

- the backup ID is malformed;
- the manifest owner or schema is wrong;
- a declared file is missing or has a digest mismatch;
- live database, secret, WAL, or SHM paths are symlinks;
- the backup root is unsafe.

A rejected rollback does not modify live state. Do not edit manifests or copied files manually.

## Exact restoration behavior

- If a database or secret existed before migration, its backed-up bytes are restored atomically.
- If apply created a new database or secret, rollback removes it.
- SQLite WAL/SHM sidecars are removed only after the backup has passed validation.
- Legacy JSON sources are not restored because apply never modifies them.

## Operator checklist

1. Run dry-run and resolve every `blocked` or `degraded` result.
2. Stop concurrent plugin processes before apply or rollback.
3. Apply and record the returned backup ID.
4. Run doctor, search, extraction, and Console smoke tests.
5. Keep the backup until the upgraded installation has operated normally.
6. Use the CLI rollback command; never copy SQLite files over a live process.

The exercised gate and exact before/after digests are documented in [Legacy-State Migration Gate](v3-state-migration-gate.md).
