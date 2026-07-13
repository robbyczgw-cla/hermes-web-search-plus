# WSP v3 — Contract Amendment 002: Source-Only Evidence Preservation

*Status: **FROZEN** · 2026-07-12 · rev 4 (engine-policy verdict: no flat shorthand; full ResultV3 shape everywhere).*
*Amends `docs/v3-contract-freeze.md`; Amendment 001 and the M0/M1 structure remain in force. On conflict, this amendment wins.*

Normative language uses **MUST / MUST NOT / MAY** per RFC 2119.

---

## 0. Versioning and charter

This amendment applies to the frozen wire value `contract_version: "3.0"`. Adoption is structural: an Amendment-002 response MUST contain `execution_id`, `observations`, and `policy_actions`, and MUST NOT contain `source_independence_estimate`. No invented `3.0.0-amendment.*` wire value exists.

WSP 3.0 is a **source-only evidence engine**. Answer synthesis, claim generation, verification judgments, watch logic, and orchestrator behavior MUST NOT exist in the engine core.

## 1. ResponseV3 top-level deltas

The following fields are REQUIRED, including when their arrays are empty:

```text
execution_id:   str
observations:   ObservationV3[]
policy_actions: PolicyActionV3[]
source_diversity: SourceDiversityV3
```

`request_id` remains caller correlation. `execution_id` identifies this engine execution and MUST be fresh for every execution, including cache hits.

`engine` is OPTIONAL in 3.0 but fully typed: when present it MUST carry all three fields — `engine: { name: str, version: str, build_commit: str }`. Partial engine objects MUST be rejected.

`timing: { total_ms }` is explicitly **deferred**: it is not freeze-blocking, MUST NOT be emitted in 3.0, and requires a later amendment before introduction.

## 2. ObservationV3 — lossless normalized evidence

Every provider-returned item MUST become an observation **before** deduplication, filtering, reranking, representative selection, or truncation. An observation is lossless with respect to descriptor-allowlisted source evidence; it is not a raw-payload escape hatch.

```text
ObservationV3 = {
  observation_id:        str,       # REQUIRED; deterministic from
                                      # (provider_attempt_id, provider_result_index)
  provider_attempt_id:   str,       # REQUIRED FK -> provider_attempts[].attempt_id
  provider_result_index: int >= 0,  # REQUIRED
  provider:              str,       # REQUIRED registry id
  endpoint_id:           str,       # REQUIRED registry endpoint id
  kind: "search_result" | "extracted_document",
  url: {
    observed:  str,                 # REQUIRED, exactly provider-returned URL
    canonical: str                  # REQUIRED, deterministic canonicalization
  },
  title: str | null,                # provider text, no rewrite
  snippet: str | null,              # literal provider text
  text: str | null,                 # literal extracted-document text
  provider_rank: int >= 1 | null,
  provider_score: {
    value: number,
    semantics: "provider_local_relevance" | "unknown"
  } | null,
  published_at: {
    raw: str,
    normalized: RFC3339 | null
  } | null,
  provider_fields: {
    <provider-id>: { <descriptor-allowlisted fields only> }
  }
}
```

Presence rules:

- `search_result`: `snippet` MUST be a string; `text` MUST be null.
- `extracted_document`: `text` MUST be a string; `snippet` MUST be null.
- `title` MAY be null for either kind.
- Invalid or relative date text (for example `Yesterday`) remains in `published_at.raw`; `normalized` MUST be null unless normalization is deterministic and valid.
- `provider_fields` MUST be descriptor-allowlisted, serialized-size-bounded to **4096 UTF-8 bytes per observation** and **131072 UTF-8 bytes aggregated per response**, and MUST NOT contain a raw response body.
- `engine_synthetic_position` is intentionally **not** a `provider_score` semantic. Engine ordering belongs in `engine_rank`.

## 3. results[] is a consumer projection

```text
ResultV3 = {
  result_id: str,
  kind: "search_result" | "extracted_document",
  engine_rank: int >= 1,
  representative_observation_id: str,
  observation_ids: non-empty str[],
  dedup_cluster_id: str,
  url: { observed: str, canonical: str },
  title: ProjectedTextV3 | null,
  snippet: ProjectedTextV3 | null,
  text: ProjectedTextV3 | null
}

ProjectedTextV3 = {
  text: str,
  text_sha256: lowercase sha256 hex of text UTF-8,
  origin: "provider" | "engine",
  provenance: {
    observation_id: str,
    source_field: "title" | "snippet" | "text",
    transformations: TransformationV3[]
  },
  segments: SegmentV3[]
}

SegmentV3 = {
  start: int >= 0,          # unicode codepoint offset, NFC text
  end:   int > start,       # half-open [start, end)
  text:  str
}
```

Every referenced observation id MUST resolve. `observation_ids` MUST contain the representative id. Every cluster member remains in `observations`; deduplication is non-destructive.

`origin: "engine"` means that the engine performed an allowed deterministic transformation. It MUST NOT mean generated or rewritten content.

**No shorthand.** Flat string content fields on a result are FORBIDDEN. Every content-bearing field (`title`, `snippet`, `text`) is `ProjectedTextV3 | null` — always carrying `provenance`, `text_sha256`, and `segments`; `url` is always `{observed, canonical}`. `ResultV3` has `additionalProperties: false`; the legacy `status`/`canonical_url`/`cluster_id`/`provenance` result fields do not exist in Amendment-002 results. `dedup_cluster_id` is REQUIRED for clustered `search_result` projections and MAY be absent for `extracted_document` or unclustered results.

**Deterministic projection.** The projection is a pure function of `observations[]`: stable sort by `(engine_rank, provider_attempt_id, provider_result_index)`. The cluster representative is chosen by fixed tie-breakers, in order: lowest `engine_rank`, then lexicographically smallest `provider_attempt_id`, then lowest `provider_result_index`. There is no rewrite, summary, merge, or claim stage anywhere in the projection.

## 4. Single-Source-Content-Invariant

Every content-bearing `ProjectedTextV3` MUST be derived from **exactly one** source field of the observation named by `provenance.observation_id`.

The exhaustive 3.0 transformation enum is:

- `whitespace_norm`
- `deterministic_truncation`
- `mechanical_segmentation`
- `image_base64_replace`

Combining multiple source snippets, paraphrasing, rewriting, summarizing, resolving contradictions, or generating claims is forbidden.

Segments MUST be contiguous, non-overlapping, half-open `[start, end)` unicode-codepoint ranges over the NFC-normalized projected text, covering it completely (identical convention to the frozen M0 extract offsets). The hash is computed over the UTF-8 bytes of the projected text:

```text
concat(segments[].text) == projected_text.text
sha256(projected_text.text UTF-8) == projected_text.text_sha256
```

`deterministic_truncation` additionally requires a `truncated_by_limit` policy action for the same observation.

## 5. PolicyActionV3

Every transformation that shapes `results[]` MUST be explicit:

```text
PolicyActionV3 = {
  action: "excluded" | "reranked" | "demoted" |
          "selected_as_representative" | "truncated_by_limit",
  observation_id: str,
  reason: "spam_domain" | "intent_authority" | "domain_diversity" |
          "dedup_representative" | "max_results" | "max_content_bytes"
}
```

The action/reason combinations are closed for 3.0:

- `excluded` → `spam_domain`
- `reranked` → `intent_authority`
- `demoted` → `domain_diversity`
- `selected_as_representative` → `dedup_representative`
- `truncated_by_limit` → `max_results | max_content_bytes`

Silent exclusion, demotion, reranking, representative selection, or truncation is a contract violation.

## 6. ProviderAttemptV3 extension

Each considered provider gets an attempt record, including skipped providers:

```text
ProviderAttemptV3 += {
  endpoint_id: str,
  decision: "attempted" | "skipped",
  skip_reason: SkipReason | null,  # unchanged frozen M0 namespace
  tries: ProviderTryV3[]
}

ProviderTryV3 = {
  try_number: int >= 1,
  started_at: RFC3339,
  duration_ms: int >= 0,
  outcome: "success" | "error",
  error: {
    error_class: ErrorClass,  # unchanged frozen M0 namespace
    code: str,
    http_status: int | null,
    retryable: bool,
    retry_after_ms: int >= 0 | null
  } | null
}
```

`decision=skipped` requires `skip_reason` and `tries=[]`. `decision=attempted` requires at least one try. This log is the source of truth for fallback and cooldown receipts.

## 7. source_diversity — honest components only

`source_independence_estimate` is removed and replaced with:

```text
SourceDiversityV3 = {
  method: str,
  method_version: str,
  method_degraded: bool,
  provider_count: int >= 0,
  host_count: int >= 0,
  source_family_count: int >= 0,
  unique_cluster_count: int >= 0
}
```

`additionalProperties` MUST be false. 3.0 has no scalar, estimate, confidence, or nullable scalar placeholder. A calibrated scalar is 3.1 scope.

## 8. Response allowlist

Canonical requests, observations, results, policy actions, and cache-reconstructed responses MUST NOT contain:

- `answer`
- `full_synthesis`
- `claim`
- `verification`
- `truth_confidence`
- `type: "synthesis"`
- bare cross-provider `score`

Unknown provider fields MUST NOT pass through automatically. `provider_fields` is the only extension point and remains descriptor-allowlisted and size-bounded.

## 9. Provider descriptor and outbound request gates

Every selectable provider mode MUST declare:

```text
output_semantics: "source_results" | "source_text"
```

Modes advertising answer, synthesis, reasoning, claim, or verification semantics MUST be rejected at registration and MUST be impossible to select through routing, fallback, shadow, or either kill switch.

Outbound requests MUST NOT contain chat messages, system-answer instructions, or answer-generation options. Specifically:

- Tavily: `include_answer=false`
- Linkup: `outputType="searchResults"` only
- Exa: never `deep` or `deep-reasoning`
- Perplexity/Kilo: fail before network I/O unless a registry-verified source-only endpoint exists

## 10. Cache origin and legacy sanitization

A verbatim `ResponseV3` MUST NOT be cached. Cache v3 stores normalized observations plus:

- `origin_execution_id`
- `origin_provider`
- `endpoint_id`
- `normalizer_version`
- `contract_version`

A hit builds a new response with a fresh `execution_id`, `cache_status.origin_execution_id`, accurate `cache_status.age_seconds` (M0 field name), and no fabricated current provider attempts.

**FK waiver for cache-served evidence.** When `cache_status.disposition` is `fresh_hit` or `stale_hit`, `observations[].provider_attempt_id` references the origin execution's attempt and MUST NOT resolve against the current `provider_attempts[]`; resolving it locally would require fabricating attempts. For all other dispositions the FK MUST resolve.

Legacy v2 files remain read-only and byte-identical. Sanitization drops banned fields, re-normalizes surviving source results, and emits:

```json
{"code":"wsp.cache.legacy_field_dropped","reason":"LEGACY_FIELD_DROPPED"}
```

If no source-only observation survives, the entry is rejected and MUST NOT be served.

## 11. Scope boundary

**3.0:** this amendment, Charter Purge, canonical request/response, lossless observations, complete attempts including skips/retries, typed errors, provenance, non-destructive dedup clusters, search cache v3 plus legacy sanitize, routing-v2 receipt, shadow-only policy interface, two-level kill switch, minimal SQLite circuit/adaptive store, internal adapter protocol, exact mechanical segments.

**3.1:** calibrated source-diversity scalar, public provider SDK, rich budget/credit preflight, extraction caching, semantic span extraction.

**Never in engine core:** answer synthesis, claim generation, verification judgments, watch logic, orchestrator behavior.

## 11a. Unchanged from M0 (explicitly re-frozen)

- `contract_version: "3.0"` wire value.
- `capability` and `status` enums.
- `routing_receipt` required fields.
- `cache_status` shape including `source_contract_version` and `age_seconds`.
- `ErrorClass`, `SkipReason`, `FallbackReason`, `CircuitState`, `AttemptOutcome` enums.
- Degrade-code vocabulary.
- Extract offsets: `unicode_codepoint` unit, NFC normalization, half-open `[start, end)` — SegmentV3 (§3) now uses the identical convention.
- Top-level `error` only when `status = failed`.
- The six M0 golden fixtures remain **semantically** valid — same evidence, status, routing, cache, and error semantics — but their `results[]` wire shape is fully migrated to Amendment-002 `ResultV3`/`ObservationV3`. Old result wire shapes are not preserved.

## 12. Release-blocking acceptance

- JSON schema and Python contract implement the exact shapes above.
- `ResponseV3.from_dict(r.to_dict()) == r` with observations, policy actions, attempts, and diversity.
- Inverted banned-field tests are green.
- Sentinel composition is green for the central projection used by Parallel and You.
- Descriptor, outbound-request, capability, formatter, legacy-cache, router, and mechanical-offset gates are green.
- Existing v2 public tools remain behaviorally compatible except that source-only Charter violations are removed.
- All six updated golden fixtures validate against the regenerated JSON schema and pass the mechanical-consistency boundary suite.
- No acceptance test may be vacuous: every gate asserts on concrete offenders or concrete shapes, never on empty iterations.
