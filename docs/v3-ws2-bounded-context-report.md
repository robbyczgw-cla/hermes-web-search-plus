# WSP v3 WS-2 — Bounded Context Contract Report

**Branch:** `feat/v3-evidence-spine`  
**Review base:** `fix/source-only-charter-purge` at `41fb087`  
**Status:** PASS candidate pending independent Andy implementation recheck  
**Date:** 2026-07-12  
**Active plugin:** unchanged

## Verdict

WS-2 implements Plan A's Bounded Context Contract on top of the Evidence Spine. Native extract-v3 now bounds URL fan-out and inline content before provider/context growth can become unbounded, reports every omission and truncation truthfully, and retains full source text in an owned age/size-bounded store for page-on-demand retrieval.

This workstream is not an RC and does not begin the Operator Console.

## Contract Amendment 003

Andy's fixture-definition gate returned **PASS** and required a small additive Amendment 003 rather than overloading frozen Amendment-002 result fields.

Added wire semantics:

- typed `limits_applied.extract` with requested/processed/omitted URL accounting;
- effective URL and content limits;
- exact inline codepoint count and truncation flag;
- optional top-level `stored_content[]` for full-text retention outcomes;
- additive policy-action reason `max_context_chars`;
- warnings `wsp.extract.urls_omitted`, `wsp.content.truncated`, and `wsp.storage.full_text_unavailable`.

M0/M1 and Amendment 002 remain structurally intact. Existing six goldens continue to validate.

Normative document:

- `docs/v3-contract-amendment-003-bounded-context.md`

## Fixture-first TDD

Acceptance fixtures were frozen before production code:

- `01_extract_within_limits.json`
- `02_urls_capped.json`
- `03_content_truncated.json`
- `04_truncated_stored.json`
- `05_storage_failed.json`
- `06_no_starvation.json`
- `07_budget_bounds.json`
- `09_single_source_after_truncation.json`

RED proof before implementation:

```text
2 collection errors
ModuleNotFoundError: No module named 'bounded_context_v3'
```

Fixture checkpoint:

```text
6fdb217 test(v3): freeze WS-2 bounded context fixtures
```

GREEN after implementation:

```text
33 WS-2 tests passed
109 focused tests + 6 subtests passed
```

## Runtime behavior

### URL fan-out

- Default/effective operator ceiling: 10 URLs.
- Request may ask for fewer.
- Hard maximum: 50.
- Effective cap is `min(requested_or_default, operator_ceiling, 50)`.
- Only the first effective URLs in request order enter provider execution.
- Omitted URLs produce neither observations nor provider execution payloads.
- Omitted URL order is retained in `limits_applied.extract.omitted_urls`.

### Inline content budget

- Operator default: 60,000 NFC Unicode codepoints.
- Per-request range: clamped to `[1,000, 200,000]`.
- Strings and booleans are rejected as invalid request values.
- Allocation uses deterministic water-filling over stable result order.
- Short results return unused share to remaining results.
- Remainders are assigned in stable order.
- A long first result cannot starve later successful results.

### Source integrity

- Provider title/snippet/text is normalized to NFC before budgeting.
- Truncated text is exactly the half-open `[0,n)` prefix of one observation.
- Provenance adds `deterministic_truncation` without rewrite or concatenation.
- Segments cover the returned inline text exactly.
- Inline SHA-256 hashes inline text; full-text SHA-256 hashes full NFC source text.
- Every truncation adds `truncated_by_limit/max_context_chars`.

### Full-text retention

Implementation: `bounded_context_v3.FullTextStore`.

- Namespace: `web_text_v3` under the configured cache root's `web/` directory.
- Public reference contains only `store`, opaque SHA-256 key, and media type.
- No absolute filesystem path appears on the wire.
- Default TTL: 604,800 seconds.
- Default owned-size ceiling: 268,435,456 bytes.
- TTL enforced on lookup/write.
- Size enforced oldest-mtime first after write.
- Only marker-owned SHA-named Markdown entries and `.wsp-v3-*.tmp` files are deletion targets.
- Foreign Markdown, binaries, provider health/stats, SQLite state, and evidence-cache files are preserved.
- Concurrent disappearance and permission failure do not replace provider results with storage errors.
- Failed storage emits null reference/hash/size and a truthful warning.

### Cache compatibility

- Legacy and native extract paths normalize identical default limits before deriving the v3 cache key.
- Evidence-cache material preserves `limits_applied` and `stored_content` origin metadata.
- Cache reads still receive fresh execution identity and neutral routing identity.

## Operator configuration

New defaults:

```json
{
  "bounded_context": {
    "max_urls": 10,
    "max_context_chars": 60000,
    "full_text_ttl_seconds": 604800,
    "full_text_max_bytes": 268435456
  }
}
```

Validation is fail-fast:

- `max_urls`: integer 1–50;
- `max_context_chars`: integer 1,000–200,000;
- TTL and max bytes: non-negative integers;
- optional `cache_root`: non-empty string.

`max_urls` is an operator ceiling. `max_context_chars` is the operator default used when a request omits its per-call budget.

## Full verification

### Clean isolated-config suite

```text
685 passed, 6 subtests passed in 7.02s
```

### Sterile and generated gates

```text
ruff: passed
compileall: passed
git diff --check: passed
provider docs drift: current
routing docs drift: current
contract schemas: current
Ajv schema boundary: passed
```

### Real isolated Serper extraction

Target: public Hermes Agent documentation page. Native v3 request used one URL and `max_context_chars=1000` with disposable cache and SQLite paths.

```json
{
  "cache_root_files": 1,
  "context_chars_returned": 1000,
  "observations": 1,
  "reference_store": "web_text_v3",
  "results": 1,
  "schema_valid": true,
  "status": "degraded",
  "storage_succeeded": true,
  "stored_content_count": 1,
  "truncated": true
}
```

No active-plugin files or live cache/state paths were changed.

## Explicit non-goals and unfinished release work

Not implemented or claimed by WS-2:

- Operator Console;
- synthesis, claims, verification, or semantic spans;
- self-hosted deployment profile;
- hosted backend;
- extraction-result caching;
- public Provider SDK;
- complete Shadow Observer, two-level kill switch, adaptive-state migration, Routing-v2 hardening, or internal adapter protocol;
- RC, migration, downstream sync, release, or active-plugin switch.

## Remaining gates

Before push/PR/merge/release:

- Andy independent implementation recheck against the final checkpoint;
- Andy independent Charter-Gate suite against the exact split branches;
- fresh-clone and isolated-config smoke;
- migration/compatibility documentation;
- Robby's explicit GO for each push, PR, merge, release, and active-plugin switch.

WS-3 Operator Console must not begin until this report and Andy's implementation verdict are finalized.
