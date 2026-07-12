# WSP v3 WS-3 — Operator Console Contract

**Status:** TASK-1 GATE CANDIDATE · 2026-07-12 · requires Robby acceptance before Task 2

This contract defines the read-only Operator Console boundary. It is additive to Contract v3 and Amendments 002/003. Classic Routing-v2 remains authoritative; shadow policy remains observational and cannot alter execution.

## Hard boundary

The Console:

- binds only to `127.0.0.1` and MUST reject a requested non-loopback bind;
- accepts only `GET` and `HEAD`; `POST`, `PUT`, `PATCH`, `DELETE`, `OPTIONS`, `CONNECT` and `TRACE` return `405` without side effects;
- requires a startup-generated token and strict loopback Host validation;
- performs no provider/network calls, config writes, cache clear/prune, benchmark runs, search or extraction;
- exposes no hosted backend, WebSocket or remote account surface.

Every response uses `Cache-Control: no-store`, `Content-Security-Policy`, `X-Content-Type-Options: nosniff`, frame denial and strict referrer policy. No wildcard CORS header is emitted.

## One privacy choke point

`operator_privacy_v3.assert_operator_payload_safe(payload)` is the single recursive validator for:

1. `/api/v3/overview` output;
2. `/api/v3/receipts` output;
3. `/api/v3/benchmark-history` output;
4. every receipt record before journal persistence.

No endpoint or writer may carry a private per-endpoint copy of this logic. The validator fails closed before serialization/write.

Forbidden field names, case-insensitive after replacing `-` with `_`:

- `query`, `url`, `urls`, `title`, `text`, `snippet`, `content`, `fulltext`, `full_text`;
- `api_key`, `secret`, `token`, `authorization`, `headers`;
- `credential_fingerprint`, `credential_slot`, `endpoint_url`;
- `path`, `file`, `filename`, `cache_dir`, `state_path`.

Forbidden string values:

- absolute POSIX/Windows filesystem paths;
- URI values containing credentials;
- bearer/basic authorization values;
- values matching configured secret material supplied to the validator.

Public opaque IDs (`execution_id`, `attempt_id`, cache `entry_id`) are allowed. Provider IDs and non-secret endpoint IDs are allowed only in the canonical v3 wire response; the Console journal omits endpoint identity entirely.

## Endpoint DTOs

All top-level DTOs require `schema_version: 1` and reject unknown top-level keys.

### `GET /api/v3/overview`

Sections:

- `engine`: contract version, plugin version, state availability;
- `providers`: provider ID/display name, capabilities, configured/key-present boolean, disabled/auto-allowed/cooldown state;
- `bounds`: effective configured defaults and hard limits;
- `cache`: owned response/fulltext counts and bytes, oldest/newest timestamps, no directories;
- `circuits`: aggregate public-state counts only;
- `receipts_summary`: count/latest timestamp;
- `benchmark_summary`: count/latest timestamp/kinds and `extract_collected` boolean.

### `GET /api/v3/receipts?limit=N`

`limit` is an integer clamped to 1–100. Records are newest first. Each record is the exact sanitized shape accepted by the journal.

### `GET /api/v3/benchmark-history?limit=N`

`limit` is an integer clamped to 1–100. Records are newest first. `kind` is `search|extract`. Historical untyped records load as `search`. Missing extraction history is represented by `extract_collected=false` / `status="not_collected"`, never by a fabricated zero-result run.

## Routing-v2 Receipt completion

The current six-field receipt is partial. WS-3A adds the following typed fields before journaling/UI:

```json
{
  "authority": "classic",
  "execution_scope": "current",
  "candidate_decisions": [],
  "cache_origin": null,
  "shadow_observation": null
}
```

### Candidate decision

Required fields:

- `provider`: registry provider ID;
- `position`: one-based stable candidate position;
- `decision`: `selected | attempted_failed | attempted_no_selection | skipped | not_attempted | origin_selected`;
- `reason_code`: one value from the closed enumeration below;
- `attempt_id`: current attempt ID or null.

Closed `reason_code` enumeration:

- `classic_selected`
- `fallback_selected`
- `attempt_failed`
- `insufficient_results`
- `blocked_auth`
- `blocked_quota`
- `circuit_open`
- `budget_denied`
- `provider_unavailable`
- `not_attempted_after_success`
- `cache_origin_selected`

Cross-field invariants:

- direct success has exactly one `selected/classic_selected`;
- fallback success has prior `attempted_failed/attempt_failed`, `attempted_no_selection/insufficient_results`, or typed skipped records and exactly one `selected/fallback_selected`;
- successful or partial attempts that produce no selected provider are recorded as `attempted_no_selection/insufficient_results`, never as failed or not attempted;
- skipped decisions map to a skipped `provider_attempts[]` item and its typed skip reason;
- current non-cache attempted/selected decisions reference current `provider_attempts[].attempt_id`;
- `origin_selected` is allowed only inside `cache_origin` and never references current attempts.

### Cache-hit separation

A cache-served current response MUST have:

- `execution_scope="current"`;
- `candidate_decisions=[]`;
- no fabricated current `provider_attempts[]`;
- `selected_provider=null` for current execution;
- non-null `cache_origin` containing origin execution ID, origin policy metadata, origin candidate order, origin selected provider, fallback reason and origin candidate decisions;
- origin attempt IDs omitted from the journal-facing DTO.

`cache_origin` is historical evidence, not a claim that a provider ran in the current execution.

### Shadow observation

When present:

```json
{
  "observed": true,
  "policy_id": "<id>",
  "policy_revision": "<revision>",
  "selected_provider": "<provider-or-null>",
  "affected_execution": false
}
```

`affected_execution` MUST be false. Shadow selection cannot alter candidate order, attempts or selected provider.

## Journal contract

The marker-owned bounded JSONL journal stores only:

- schema version and timestamp;
- execution ID, capability and response status;
- sanitized completed receipt;
- cache disposition;
- WS-2 numeric/count/boolean limits summary;
- warning/error codes.

The shared privacy choke point runs immediately before append. Journal failure is best-effort and cannot change the provider response. Retention targets only the one marker-owned journal and never scans/deletes foreign files.

## Acceptance fixtures

- `tests/fixtures/v3/ws3/overview.json`
- `tests/fixtures/v3/ws3/receipts.json`
- `tests/fixtures/v3/ws3/benchmark-history.json`

Release-blocking tests require:

- one shared recursive privacy choke point over all endpoint DTOs and journal records;
- explicit refusal of `0.0.0.0`, `::`, LAN, Tailnet and arbitrary host binds;
- cache-hit origin/current separation with zero fabricated current attempts;
- closed reason-code validation and candidate/attempt consistency;
- Amendment-002 and WS-2 fixture additivity;
- no Console production code before this Task-1 gate is accepted by Robby.
