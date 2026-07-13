# Migrating to Web Search Plus 3.0

WSP 3.0 keeps the public `web_search_plus` and `web_extract_plus` tool names, but moves execution, cache ownership, receipts, and operational state onto the v3 evidence spine. The migration command is deliberately dry-run first and never deletes the legacy JSON sources.

## Before upgrading

1. Keep a copy of the installed plugin directory and configuration.
2. Confirm the current installation is healthy:

```bash
python3 search.py doctor --json
```

3. Stop any process currently using the plugin cache before applying the state migration.

## Preview the state migration

From the plugin directory:

```bash
python3 search.py state-migrate
```

The command reads `provider_health.json` and `provider_stats.json`, validates the target SQLite path, and prints one path-free JSON report. Dry-run is the default. It does not create a database, secret, or backup.

A successful preview reports `"status":"ready"`. A previously imported source reports `"status":"unchanged"`.

## Apply

```bash
python3 search.py state-migrate --apply
```

Apply creates a verified backup before the first SQLite write, imports both legacy state sources in one transaction, and returns a `backup_id`. Save that ID until the upgraded installation has passed its smoke tests.

The source JSON files are preserved byte-for-byte. Re-running apply with the same source digest is idempotent and reports `unchanged`.

## Verify

```bash
python3 search.py doctor --json
python3 search.py --query "Web Search Plus migration smoke" --provider auto --quality-report
python3 search.py --extract-urls https://example.com --provider auto
```

Also verify the local Operator Console if you use it:

```bash
python3 ui.py --port 8765
```

## Roll back the imported state

```bash
python3 search.py state-migrate --rollback BACKUP_ID
```

Rollback verifies the manifest and every backup digest before changing live state. It restores the exact pre-migration SQLite database and local state secret. If neither existed before apply, rollback removes the files created by the migration.

The legacy JSON sources are not rewritten during apply, so rollback does not need to restore them.

See [Backup and Restore](V3_BACKUP_RESTORE.md) for storage ownership and failure behavior.

## Compatibility changes

- `web_search_plus` and `web_extract_plus` remain the only public tools.
- WSP 3.0 is source-only. Answer synthesis, claim generation, and verification judgments are not part of the plugin.
- Native Perplexity and Kilo Perplexity answer endpoints are no longer registered as search providers because they do not expose a verified source-only mode.
- Classic Routing v2 remains authoritative. New policy observation cannot affect provider execution in 3.0.
- Legacy cache entries may be read through the compatibility path, but banned synthesis fields are dropped and valid source results are re-normalized.

The complete surface matrix is in [3.0 Compatibility](V3_COMPATIBILITY.md).
