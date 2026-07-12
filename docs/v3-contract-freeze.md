# Web Search Plus v3 contract freeze — M0 engine side

Status: **M0 FROZEN — engine-owner and policy-owner co-signed.** The field names, enum namespaces, projection/cache rules, six golden fixtures, and resolved joint decisions below are normative for M1. Any change now requires an explicit contract amendment and fixture update.

Canonical artifacts:

- `contract_v3.py` — Python DTOs and enum namespace
- `schemas/v3/request.schema.json` — self-contained Draft 2020-12 RequestV3 schema
- `schemas/v3/response.schema.json` — self-contained Draft 2020-12 ResponseV3 schema
- `scripts/gen_contract_v3_schemas.py` — schema generator sourcing enum values from the DTO module

## Charter boundary

The engine exposes exactly two capabilities:

- `search`
- `extract`

It does not synthesize answers, judge truth, generate claims, verify claims, or watch sources. `verify` and `watch` may consume this contract from separate components but are never values of `Capability`.

Mechanical extraction offsets are in 3.0. Semantic spans, claim/evidence mapping, schema-directed extraction, and relevance interpretation are 3.1 or later.

## RequestV3 field freeze

Required:

- `contract_version: "3.0"`
- `capability: "search" | "extract"`
- `input: SearchInput | ExtractInput`

Optional:

- `request_id: string` — caller-supplied correlation ID; engine generates one when absent
- `options: SearchOptions | ExtractOptions`
- `cache: CacheRequest`
- `routing: RoutingRequest`
- `budget: BudgetRequest`
- `client: ClientNegotiation`

Unknown top-level and nested fields are rejected.

### SearchInput

Required:

- `query: string`, non-empty

Forbidden:

- `urls`

### ExtractInput

Required:

- `urls: URI[]`, 1–32 unique URLs

Forbidden:

- `query`

### SearchOptions

All optional:

- `max_results: integer`, 1–50
- `freshness: day | week | month | year`
- `time_range: day | week | month | year`
- `search_type: search | news`
- `include_domains: string[]`
- `exclude_domains: string[]`
- `locale.country: ISO-3166 alpha-2`
- `locale.language: ISO-639-1`

### ExtractOptions

All optional:

- `output_format: markdown | html`
- `include_images: boolean`
- `include_raw_html: boolean`
- `render_js: boolean`

### CacheRequest

All optional:

- `mode: prefer | bypass | only`
- `ttl_seconds: integer >= 0`
- `allow_stale_seconds: integer >= 0`

`only` never performs provider egress. A miss in `only` mode produces a failed response with an explicit cache error.

### RoutingRequest

All optional:

- `mode: auto | fixed`
- `provider: string`
- `allow_fallback: boolean`
- `policy_mode: classic | shadow`

`shadow` never changes execution order. Classic remains authoritative in 3.0. Fixed mode requires a provider at semantic-validation time.

### BudgetRequest

All optional:

- `max_provider_attempts: integer`, 1–32
- `max_wall_time_ms: integer >= 1`
- `max_cost_microunits: integer >= 0`

`max_cost_microunits` is enforced only from operator/provider-known costs. Unknown provider costs are not invented; the admission decision and limitation are reported.

### ClientNegotiation

All optional:

- `accept_contract_versions: unique array of "3.0" | "2.x"`
- `accept_features: unique array of:`
  - `provider_attempts`
  - `dedup_clusters`
  - `source_independence_estimate`
  - `mechanical_text_offsets`
  - `stale_cache`

## ResponseV3 field freeze

Required on every response:

- `contract_version: "3.0"`
- `request_id: string`
- `capability: search | extract`
- `status: ok | degraded | failed`
- `results: SearchResultV3[] | ExtractResultV3[]`
- `provider_attempts: ProviderAttemptV3[]`
- `routing_receipt: RoutingReceipt`
- `cache_status: CacheStatus`
- `limits_applied: object`
- `dedup_clusters: object[]`
- `warnings: WarningV3[]`

Optional:

- `source_independence_estimate: IndependenceEstimate`
- `error: ErrorV3` — required only for `status=failed`, forbidden otherwise

### SearchResultV3

Required:

- `result_id: string`
- `status: "ok"`
- `title: string`
- `url: URI`
- `canonical_url: URI`
- `provenance: ProvenanceObservation[]`, non-empty

Optional:

- `snippet: string`
- `published_at: RFC-3339 date-time`
- `cluster_id: string`

### ExtractResultV3

Required for all items:

- `result_id: string`
- `status: ok | failed`
- `url: URI`
- `canonical_url: URI`
- `provenance: ProvenanceObservation[]`, non-empty

Required when `status=ok`:

- `text: string`
- `offset_unit: "unicode_codepoint"`
- `text_normalization: "NFC"`
- `segments: TextSegmentV3[]`

Required when `status=failed`:

- `error: ErrorV3`

Before offsets are calculated, `text` is normalized to Unicode NFC. A segment contains only `segment_id`, `start`, and `end`. It does not contain a claim, relevance score, semantic type, interpretation, or generated summary. Offsets are zero-based Unicode code-point indexes into that exact NFC string and use half-open intervals `[start, end)`.

### ProvenanceObservation

Required:

- `provider: string`
- `source_url: URI`
- `retrieved_at: RFC-3339 date-time`

Optional:

- `provider_rank: integer >= 1`
- `provider_result_id: string`

### RoutingReceipt

Required:

- `policy_id: string`
- `policy_revision: string`
- `mode: classic | shadow`
- `candidate_order: string[]`
- `selected_provider: string | null`
- `fallback_reason: FallbackReason`

Optional:

- `shadow: object` — proposed decision and comparison metadata only; never authoritative execution state

### CacheStatus

Required:

- `disposition: fresh_hit | stale_hit | miss | bypassed | unavailable`

Optional:

- `entry_id: string`
- `age_seconds: integer >= 0`
- `ttl_seconds: integer >= 0`
- `served_stale: boolean`
- `source_contract_version: 3.0 | 2.x`
- `write_error: string`

## Frozen error and attempt namespace

### ErrorClass

- `invalid_request`
- `unsupported`
- `config`
- `auth`
- `quota`
- `rate_limit`
- `transient`
- `timeout`
- `provider_contract`
- `content`
- `security`
- `budget`
- `cancelled`
- `internal`

### ErrorV3

Required:

- `error_class: ErrorClass`
- `code: string` matching `wsp.<namespace>.<specific>`
- `message: non-empty string`
- `retryable: boolean`

Optional:

- `provider: string`
- `http_status: 100–599`
- `retry_after_seconds: number >= 0`
- `details: object`

Provider-native codes may appear only under `details.provider_code`; they never become stable WSP error codes.

### AttemptOutcome

- `success`
- `partial`
- `skipped`
- `failed`
- `cancelled`

A failed attempt requires `error`. A skipped attempt requires `skip_reason`.

### SkipReason

- `disabled`
- `unsupported_capability`
- `not_configured`
- `missing_credentials`
- `auth_blocked`
- `quota_blocked`
- `rate_limited`
- `circuit_open`
- `budget_blocked`
- `policy_excluded`
- `deadline_exceeded`

### FallbackReason

- `none`
- `selected_failed`
- `selected_skipped`
- `insufficient_results`
- `partial_content`
- `budget_chain`

### CircuitState

- `closed`
- `open`
- `half_open`
- `blocked_auth`
- `blocked_quota`
- `unknown`

## v2 compatibility projection rules

There is one v3 execution pipeline. Compatibility is projection only:

```text
legacy request -> RequestV3 -> v3 engine -> ResponseV3 -> legacy response
```

### Legacy request projection

- Existing search CLI/tool fields map into `SearchInput`, `SearchOptions`, `CacheRequest`, and `RoutingRequest`.
- Existing extract fields map into `ExtractInput` and `ExtractOptions`.
- Missing v3-only fields receive engine defaults.
- Explicit-provider calls preserve current strict/fallback semantics.
- Invalid legacy combinations fail before provider egress.

### Legacy search response projection

- `results` maps from SearchResultV3 with v3-only IDs/provenance removed unless already representable.
- `provider` is the selected successful provider.
- Existing `routing` is projected from RoutingReceipt and ProviderAttemptV3.
- `fallback_errors` is projected from failed attempts.
- `cooldown_skips` is projected from skipped attempts with `circuit_open`.
- `cached` and `cache_age_seconds` are projected from CacheStatus.
- `quality_report` remains a projection/output option, not an alternate execution path.

### Legacy extract response projection

- `results[].content` is projected from `ExtractResultV3.text`.
- Existing per-result errors are projected from failed ExtractResultV3 items.
- Existing `routing.provider`, `requested_provider`, `fallback_used`, and `fallback_errors` derive from RoutingReceipt and ProviderAttemptV3.

Projection must be deterministic and side-effect free. Legacy projections never write legacy cache entries.

## Cache-v3 migration rules

1. v3 cache keys are namespaced and include `contract_version`, capability, normalized request, output-affecting options, locale, bounds, and the authoritative classic provider plan where it affects content.
2. Cache control, debug verbosity, telemetry settings, request IDs, and shadow-policy output do not alter content identity and therefore do not enter the content key.
3. v3 writes only v3 envelopes. It never mutates or overwrites v2 files.
4. A v2 search envelope may be read through a best-effort legacy reader when its marker set is complete and its request semantics can be represented losslessly.
5. A legacy hit returns `source_contract_version="2.x"`; it is not silently rewritten during the request.
6. Extract full-text files require v3 sidecar metadata before they can satisfy mechanical-offset or provenance guarantees. Bare v2 markdown files cannot masquerade as full v3 cache hits.
7. Stale data is served only when the request allows it. The response becomes `degraded`, with `disposition="stale_hit"`, `served_stale=true`, and a warning.
8. Cache corruption is `unavailable`, never a provider-health failure.
9. Cache clear/stats operate only on positively identified WSP-owned envelopes and preserve health, telemetry, SQLite state, and foreign files.
10. No eager cache migration. Entries migrate naturally on new successful writes.

## Resolved joint decisions for M0 sign-off

1. **Offset semantics:** Unicode NFC text, zero-based Unicode code-point indexes, half-open `[start,end)` intervals.
2. **Degraded semantics:** successful fallback alone remains `ok`; stale serving, omitted URLs, truncation, budget-limited execution, partial extraction, or reduced fingerprinting become `degraded`. Every degraded response carries at least one enumerated degrade reason in `warnings[].code`.
3. **Error code registry:** stable engine-owned `wsp.<namespace>.<specific>` codes, with provider-native codes only in `details.provider_code`.
4. **Telemetry privacy:** no raw query, URL, snippet, extracted text, API key, endpoint credentials, or raw provider body in persistent routing telemetry by default. Store a rotating-salt request fingerprint plus derived routing features. Explicit debug capture is opt-in and TTL-bound.
5. **Legacy cache read window:** support compatible v2 reads through the 3.0 minor line, remove no earlier than 3.1 with measured usage and release notes.
6. **Source-independence omission:** omit the object when no trustworthy estimate can be computed; never emit a fake zero. URL-only mode may emit a real estimate with `method_degraded=true`.

### Enumerated degrade warning codes

- `wsp.cache.served_stale`
- `wsp.content.truncated`
- `wsp.extract.urls_omitted`
- `wsp.extract.partial`
- `wsp.budget.limited`
- `wsp.independence.method_degraded`

## M0 exit test contract

The policy-owner's six fixtures must each validate against `response.schema.json`:

- search success
- extract success
- cache hit
- fallback
- degraded
- total failure

Boundary tests must additionally prove:

- `Capability` rejects `verify`, `watch`, and `answer`
- Extract segments contain only mechanical offsets
- top-level `error` appears only on failed responses
- provider-native errors cannot expand the stable ErrorClass enum
- shadow routing cannot change `candidate_order` used for execution
- M0 proves the contract-level single-authority invariant: only `classic` and `shadow` modes exist, and shadow never controls execution. M1 must add the runtime proof that v2 projection is deterministic and does not create a second execution path.
