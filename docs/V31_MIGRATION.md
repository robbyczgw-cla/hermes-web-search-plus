# Migrating to Web Search Plus 3.1

Coming from 3.0.x, there is nothing you must do: 3.1 keeps the public tools,
call style, and default behavior of 3.0.2, and upgrades its operational state
schema (v2 → v3) automatically and additively on first use. Coming from 2.x,
follow `docs/V3_MIGRATION.md` first.

## What upgrades automatically

- SQLite operational state: `user_version=3` is set in place by idempotent DDL.
  Existing circuit, ledger, and adaptive-sample data is untouched. No dry-run
  command is needed for this step. 5.0 no longer creates
  `shadow_evaluations_v3`. Databases that already have that table keep it;
  nothing reads or writes it.
- Receipts, schemas, and Console DTOs: new fields appear only when the
  corresponding feature is enabled.

## Opting into the 3.1 features

Each feature is independent; enable only what you want.

| Feature | Config | Off means |
|---|---|---|
| Shadow Observer | removed in 5.0 | `routing.policy_mode: "shadow"` is accepted and treated as `"classic"`. `routing_receipt.shadow_observation` is always `null`. |
| Budget Preflight | `budget_preflight.enabled: true` + limits | no checks, no receipt actions |
| Diversity rerank | `quality.diversity.rerank: true` | diagnosis only, ordering unchanged |
| Self-hosted profile | `profile: "self_hosted"` | standard provider pools |
| Semantic spans | per-call `spans: true` | responses unchanged |
| Provider SDK discovery | drop a module into `providers.d/` | built-ins only |

Kill switches (environment, always win over config):
`WSP_BUDGET_PREFLIGHT_OFF=1`. `WSP_ROUTING_CLASSIC_ONLY` is still accepted and does nothing; routing is always Classic.

## Operational notes

- The WAL checkpoint-on-close mitigation requires a Python/SQLite build that
  exposes `SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE`; on older builds behavior matches
  3.0.2 (the mitigation is a no-op).
- `budget_preflight.max_daily_provider_calls` is a soft pre-check: the
  authoritative, atomic reservation happens per attempt. Under high
  concurrency a request may pass preflight and still be stopped at attempt
  time with a typed budget error.
- Preflight's daily-quota check reads the state database read-only; on
  filesystems where a live-WAL database cannot be opened read-only, the check
  fails closed (with `on_exceed: "abort"` this rejects requests). Use
  `degrade` or disable the daily limit if your filesystem cannot support it.

## Verifying the upgrade

```bash
python3 setup.py status          # provider surface must still show 12 search / 8 extract
python3 ui.py --port 8765        # Console: /api/v3/overview must render
python3 -m pytest tests -q       # if you run from a checkout
```

## Rolling back

3.1 → 3.0.2 rollback is safe: the state schema is additive, so 3.0.2 ignores
an extra `shadow_evaluations_v3` table when one is already present. 5.0 does
not create that table. Remove any 3.1-only config keys (`profile`,
`budget_preflight`, `quality.diversity`) before downgrading to avoid
config-validation warnings. `routing.policy_mode: "shadow"` can stay; 5.0
treats it as `"classic"`.
