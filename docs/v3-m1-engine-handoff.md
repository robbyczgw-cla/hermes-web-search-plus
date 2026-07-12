# WSP 3.0 M1 engine handoff — canonical v3 runtime and v2 projection

Status: **engine implementation candidate**. Local only; no push or PR.

## As-built projection and execution signatures

```python
legacy_request_to_v3(
    capability: Capability | str,
    payload: Mapping[str, Any],
    *,
    request_id: str | None = None,
) -> RequestV3

v3_response_to_legacy_search(execution: ExecutedV3) -> Dict[str, Any]
v3_response_to_legacy_extract(execution: ExecutedV3) -> Dict[str, Any]

execute_v3_request(
    request: RequestV3,
    adapter: CapabilityAdapter,
    config: Dict[str, Any] | None = None,
) -> ExecutedV3

run_search_request_v3(
    request: RequestV3,
    *,
    config: Optional[Dict[str, Any]] = None,
) -> ResponseV3

run_extract_request_v3(
    request: RequestV3,
    *,
    config: Optional[Dict[str, Any]] = None,
) -> ResponseV3
```

`ExecutedV3` is an internal, non-wire envelope containing the frozen `ResponseV3`, the authoritative `ProviderPlan`, the untouched legacy payload, and the canonical stage trace. Its legacy payload is deep-copied on construction and projection. It is never serialized into `ResponseV3`.

## Single-path invariant

Hermes `run_search_request()` and `run_extract_request()` perform exactly:

```text
legacy input → legacy_request_to_v3() → execute_v3_request()
             → ExecutedV3 → pure legacy projection
```

Native callers start at `execute_v3_request()` and receive its `ResponseV3`. Both modes use the same capability adapter and private provider core. B6 asserts identical `ProviderPlan` and routing receipt for an equivalent legacy-projected/native `RequestV3`, and counts one private-core invocation per request.

The standalone CLI parser retains its historical import-compatible core seam for provider-specific CLI-only knobs. It uses the same private provider core but is not promoted into the provider-agnostic v3 wire surface.

Legacy v2 cache behavior under the v3 runtime:

- compatible v2 cache reads remain enabled and become `source_contract_version: "2.x"`;
- v3-generated parser arguments set `_v3_no_legacy_cache_write=True`;
- the private search core therefore never calls `cache_put()` for a v3/legacy-projected runtime request;
- projections themselves perform no cache, provider-health, telemetry, or filesystem operations.

## Round-trip examples

### 1. Search success

Legacy input:

```json
{"query":"latest model","provider":"serper","count":3,"freshness":"week"}
```

Projected request:

```json
{
  "contract_version":"3.0",
  "capability":"search",
  "input":{"query":"latest model"},
  "options":{"max_results":3,"depth":"normal","mode":"normal","quality_report":false,"research_time_budget":55.0,"freshness":"week"},
  "cache":{"mode":"prefer","ttl_seconds":3600},
  "routing":{"mode":"fixed","provider":"serper","allow_fallback":false,"policy_mode":"classic"},
  "client":{"accept_contract_versions":["3.0","2.x"]}
}
```

The provider core runs once. `ResponseV3.results[]` receives canonical URL, deterministic result ID and provenance. `v3_response_to_legacy_search()` returns the original core payload byte-equivalently.

### 2. Extract success

Legacy input:

```json
{"urls":["https://example.com/a"],"provider":"linkup","format":"markdown","include_images":false}
```

Projected request:

```json
{
  "contract_version":"3.0",
  "capability":"extract",
  "input":{"urls":["https://example.com/a"]},
  "options":{"output_format":"markdown","include_images":false,"include_raw_html":false,"render_js":false},
  "cache":{"mode":"prefer","ttl_seconds":3600},
  "routing":{"mode":"fixed","provider":"linkup","allow_fallback":true,"policy_mode":"classic"},
  "client":{"accept_contract_versions":["3.0","2.x"]}
}
```

The native result normalizes text to NFC and emits Unicode-codepoint `[start,end)` segments. The legacy projection returns the unmodified provider-core payload.

### 3. Fallback

Legacy input:

```json
{"query":"current facts","provider":"auto","count":5}
```

If the classic plan is `("you", "serper")`, You fails, and Serper succeeds, `ResponseV3.routing_receipt` remains authoritative:

```json
{
  "mode":"classic",
  "candidate_order":["you","serper"],
  "selected_provider":"serper",
  "fallback_reason":"selected_failed"
}
```

`provider_attempts` records failed You then successful Serper. The legacy projection retains the existing `routing.fallback_used` and `routing.fallback_errors` structure exactly.

### 4. Compatible v2 cache hit

Legacy input:

```json
{"query":"cached query","provider":"serper","count":5}
```

A compatible v2 cache read maps to:

```json
{"disposition":"fresh_hit","source_contract_version":"2.x","age_seconds":12}
```

No v2 cache write occurs. The legacy projection retains `cached:true` and `cache_age_seconds` exactly as returned by the existing core.

## Projection matrix

### Losslessly projected from legacy request

- capability (`search` or `extract`)
- query or URLs
- requested provider and auto/fixed routing mode
- fallback preference
- result count
- freshness, time range and search vertical
- include/exclude domains
- country and language
- public depth, normal/research mode, quality-report flag and research budget
- extract format, image/raw-HTML/render-JS flags
- cache bypass/prefer mode and TTL

### Default-filled when absent

- `contract_version = "3.0"`
- `routing.policy_mode = "classic"`
- `provider = "auto"`
- `max_results = 5`
- `depth = "normal"`
- `mode = "normal"`
- `quality_report = false`
- `research_time_budget = 55.0`
- extract `output_format = "markdown"`
- extract booleans = `false`
- cache `mode = "prefer"`, `ttl_seconds = 3600`
- client accepts `3.0` and `2.x`

### Losslessly projected to legacy response

Every field and insertion order in the private provider-core payload, including provider-specific result fields, routing diagnostics, metadata, quality report, images, fallback errors, cache flags and extract content. The projection returns a deep copy, so caller mutation cannot alter the stored envelope or a later projection.

## Amendment 001 requiring policy acceptance

M0 lacked four public v2 search inputs. `docs/v3-contract-amendment-001-m1-search-parity.md` adds strict optional fields for `depth`, `mode`, `quality_report`, and `research_time_budget`. Without them, B6 would require hidden runtime context. ResponseV3 and all six M0 golden response fixtures remain unchanged.
