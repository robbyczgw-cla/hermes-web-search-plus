# WSP v3 WS-1 — Lossless Observations, Complete Attempts, State and Cache

**Status:** PASS candidate after independent blocker fixes · 2026-07-12  
**Branch:** `feat/v3-rc-m2-m6`  
**Base:** `175da61 feat(v3): enforce source-only evidence contract`  
**Push / PR / cutover:** none

## Delivered

### 1. Complete provider attempt receipts

- Stable `endpoint_id` (`provider:capability`) on attempted and skipped records.
- Every network try is represented in dense `tries[]` order.
- Retry history preserves typed errors, HTTP status, retryability and `retry_after_ms`.
- Successful tries and terminal failed tries carry measured timestamps/durations.
- Every candidate in the authoritative plan receives a receipt.
- Lower-ranked candidates not executed after success receive `decision=skipped`, `skip_reason=policy_excluded`, and `tries=[]`.
- Search and extract use the same receipt contract.

### 2. Observation / attempt foreign-key integrity

- Authoritative engine attempts are injected into normalization before observations are constructed.
- Non-cache observations reference an attempt ID present in the delivered `provider_attempts[]`.
- Cache hits retain origin attempt IDs under the frozen cache FK waiver and fabricate no current attempts.

### 3. Lossless search observations

- Engine-owned search snapshots descriptor-normalized provider results immediately after adapter return and before spam filtering, domain reranking or projection.
- Policy-filtered provider items remain in `observations[]`.
- `results[]` is projected only from the post-policy selection.
- Filtered spam observations receive explicit `excluded / spam_domain` policy actions.
- Canonically identical observations remain non-destructive cluster members; result projection selects one representative while retaining all member IDs.

### 4. Honest v3 evidence cache

- Cache schema moved from snapshot schema 1 to evidence schema 2.
- Stored payload is cache-owned evidence material, not a verbatim `ResponseV3`.
- Stored material omits `request_id`, current `execution_id`, `provider_attempts`, and `cache_status`.
- It records `origin_execution_id`, `origin_provider`, `endpoint_id`, `normalizer_version`, normalized observations and deterministic projection material.
- Cache hits build a fresh response with a fresh execution ID, accurate origin ID/age/disposition, and zero fabricated current attempts.
- Legacy source-only projection is rebuilt from normalized evidence rather than persisted legacy response bytes.
- Old v3 snapshot entries fail closed as misses and are replaced at the same namespaced path after a successful provider execution; v2 legacy files remain untouched.

### 5. Concurrency-safe state and budget reconciliation

- SQLite continues to use WAL and `BEGIN IMMEDIATE` for atomic reservation.
- Concurrent reservation test proves 20 workers cannot overrun a five-unit budget.
- New reconciliation atomically commits actual units and releases unused reserved units.
- Reconciliation rejects negative cost, actual cost greater than reservation, and missing reservations.

### 6. Independent-review blocker fixes

- SQLite initialization or mid-flight loss no longer prevents provider execution or discards a provider outcome.
- State-unavailable executions carry `budget_decision=store_unavailable` and an explicit `wsp.state.store_unavailable` warning; known persisted auth/quota/circuit blocks remain fail-closed.
- Credential fingerprints are HMAC-SHA256 identities using a DB-local 32-byte secret, atomically created beside SQLite with mode `0600`.
- Attempt IDs are random per execution and cannot collide merely because two calls begin in the same second.
- Cache-served responses emit a neutral current routing receipt (`candidate_order=[]`, `selected_provider=null`) instead of replaying the origin routing decision as current state.

## Verification

### Full clean-config suite

```text
636 passed, 6 subtests passed in 6.64s
```

### Sterile gates

```text
ruff check: passed
compileall: passed
git diff --check: passed
contract schema drift check: current
Ajv schema boundary: all checks passed
```

### Live Serper origin + cache smoke

```text
origin: status=ok, results=3, observations=3, attempts=1
endpoint_id=serper:search, tries=1, observation FK valid
cache disposition=miss

hit: status=ok, results=3, observations=3, attempts=0
cache disposition=fresh_hit
fresh execution ID=true, origin_execution_id matches=true
routing receipt neutral=true
state HMAC secret=32 bytes, mode 0600
both responses validate against response.schema.json
```

## Changed files

- `attempt_engine_v3.py`
- `cache_v3.py`
- `extract.py`
- `orchestrator_v3.py`
- `runtime_v3.py`
- `search.py`
- `state_store_v3.py`
- focused M2/M3/v3 entrypoint tests

## Release verdict

WS-1 implementation satisfies the Amendment-002 lossless-observation, complete-attempt, cache-origin-honesty and minimal concurrency-safe state requirements covered by this workstream. Andy's initial two blockers (SQLite availability semantics and plain credential hashing) were fixed and regression-tested; the independent recheck returned PASS. Local commit is authorized; push, PR and cutover are not.
