# WSP 3.0 Legacy-State Migration Gate

**Status:** PASS
**Date:** 2026-07-13
**Scope:** isolated fixture; no user cache, credentials, or live provider calls

## Contract exercised

- `state-migrate` defaults to dry-run.
- `provider_health.json` and `provider_stats.json` are imported in one SQLite transaction.
- A verified backup is created before the first target write.
- Existing SQLite operational state survives the import.
- Rollback restores the pre-migration logical database and leaves both JSON sources byte-identical.
- Reports contain only status, counts, digests, and a backup ID; they expose no local paths or raw legacy errors.

## Dry-run evidence

Command:

```bash
python3 search.py state-migrate
```

Exit: `0`

```json
{"action":"dry_run","adaptive_providers":1,"adaptive_samples":1,"dry_run":true,"health_providers":1,"source_digest":"32f6f244f7ecf79b4dba4e582fc73ff2b31b325bf6217ff2ec73a126067e94aa","sqlite_available":true,"status":"ready"}
```

No database, secret, backup, or source bytes changed during this command.

## Apply evidence

Command:

```bash
python3 search.py state-migrate --apply
```

Exit: `0`

```json
{"action":"apply","adaptive_providers":1,"adaptive_samples":1,"backup_id":"20260713T161904Z-32f6f244f7ec","dry_run":false,"health_providers":1,"source_digest":"32f6f244f7ecf79b4dba4e582fc73ff2b31b325bf6217ff2ec73a126067e94aa","sqlite_available":true,"status":"applied"}
```

Post-apply SQLite counts:

```json
{"adaptive_samples_v3":1,"existing_budget_rows":1,"legacy_provider_health":1,"raw_error_columns":0}
```

The pre-existing budget row proves the migration did not replace unrelated operational state.

## Rollback evidence

Command:

```bash
python3 search.py state-migrate --rollback 20260713T161904Z-32f6f244f7ec
```

Exit: `0`

```json
{"action":"rollback","adaptive_providers":1,"adaptive_samples":1,"backup_id":"20260713T161904Z-32f6f244f7ec","dry_run":false,"health_providers":1,"source_digest":"32f6f244f7ecf79b4dba4e582fc73ff2b31b325bf6217ff2ec73a126067e94aa","sqlite_available":true,"status":"rolled_back"}
```

Before and after rollback:

```json
{
  "db_logical_sha256": "366c3544405a405ce01ec4360420431d7dbe2ccd948d20866e4bdd75bf1497c1",
  "provider_health_sha256": "8ca40d6e59ef9e0df144c14f16071104aeb09e189a6d044633fa3da2867f412f",
  "provider_stats_sha256": "2e00f6ad2ee4504e91f07d333cb75850576c2664248e6c330170c030309e3f8a"
}
```

All three hashes matched exactly after rollback: `rollback_exact=true`.

## Automated coverage

The migration suite additionally proves:

- schema initialization and upgrades are idempotent;
- running the same migration twice performs no second write or backup;
- duplicate adaptive samples remain distinguishable by source index;
- raw `last_error` text is not represented in the SQLite schema;
- a forced second-table write failure rolls back both imported tables;
- a corrupt SQLite target returns `degraded` without modifying any file;
- malformed legacy JSON blocks before database creation;
- a tampered backup is rejected without touching live state;
- a source change after backup blocks before the first SQLite write;
- live state, secret, WAL, and SHM symlinks block rollback;
- rollback removes a database and local secret created by the migration when neither existed before.
