# WSP v3 — Contract Amendment 003: Bounded Context

**Status:** FROZEN fixture gate · 2026-07-12 · Andy (policy owner) + Hermi (engine co-sign)

Amends `docs/v3-contract-amendment-002-source-only-evidence.md`. M0/M1 and Amendment 002 remain unchanged; this amendment is additive. On conflict, this amendment wins.

## Scope

WS-2 adds deterministic URL fan-out limits, a per-call inline content budget, truthful omission/truncation metadata, full-text page-on-demand storage and bounded retention. It adds no synthesis, claims, semantic spans, Console behavior or provider protocol.

## Request rules

For `capability=extract`:

- `options.max_urls` MAY request a lower cap. Effective cap is `min(requested_or_default, operator_ceiling, 50)`. Default is 10; values below 1 are clamped to 1.
- `options.max_context_chars` MAY set the inline budget in NFC Unicode codepoints. Default is 60000; values are clamped to `[1000, 200000]`; non-integers and booleans are invalid requests.
- The engine MUST process exactly the first `effective_max_urls` in request order. Omitted URLs MUST generate no observation or provider execution attributable to those URLs.

## Response additions

For extract responses, `limits_applied.extract` is required:

```json
{
  "requested_url_count": 12,
  "processed_urls": ["https://example.test/01"],
  "omitted_urls": ["https://example.test/11"],
  "omitted_url_count": 2,
  "max_urls": 10,
  "max_context_chars": 60000,
  "context_chars_returned": 42000,
  "truncated": true
}
```

Required invariants:

- `omitted_url_count == len(omitted_urls)`.
- `requested_url_count == len(processed_urls) + omitted_url_count`.
- `processed_urls` and `omitted_urls` preserve request order.
- `context_chars_returned` equals the sum of inline `result.text.text` codepoints.
- `truncated` is true iff at least one inline text was deterministically truncated.

Top-level `stored_content[]` is optional for Amendment-002 compatibility; Amendment-003 runtimes emit it as an empty array when no content was truncated. A truncated observation requires one entry:

```json
{
  "observation_id": "obs_...",
  "storage_attempted": true,
  "storage_succeeded": true,
  "reference": {
    "store": "web_text_v3",
    "key": "<64 lowercase hex>",
    "media_type": "text/markdown"
  },
  "full_text_sha256": "<64 lowercase hex>",
  "full_text_chars": 150000
}
```

If storage fails, `storage_succeeded=false`, and `reference`, `full_text_sha256`, `full_text_chars` MUST all be null. No absolute path may appear on the wire.

## Status and warnings

- Any omitted URL: `status=degraded`, warning `wsp.extract.urls_omitted`.
- Any inline truncation: `status=degraded`, warning `wsp.content.truncated`.
- Provider failure for a processed URL: `wsp.extract.partial`.
- Storage failure adds `wsp.storage.full_text_unavailable`; it does not by itself create degraded status, but truncation already does.
- Multiple warnings may coexist.

## Deterministic truncation

Inputs are normalized to NFC. Allocation uses deterministic water-filling over content-bearing results sorted by `engine_rank`, then the representative observation's `provider_result_index`:

1. Divide remaining budget equally among unsatisfied results.
2. Fully satisfy results shorter than their share and return unused budget.
3. Redistribute in the same stable order until no budget remains or all results are complete.
4. Any indivisible remainder is assigned one codepoint at a time in stable order.

Truncated inline text MUST be the `[0,n)` prefix of exactly one source observation. Provenance appends `deterministic_truncation`; segments cover the truncated text; inline SHA-256 hashes inline text. Stored full-text SHA-256 hashes the full NFC source.

Policy action `truncated_by_limit` gains additive reason `max_context_chars`.

## Retention

Store namespace is `web_text_v3` under the configured web cache root. Public references use only `{store,key,media_type}`.

Defaults:

- TTL: 604800 seconds.
- Maximum owned bytes: 268435456.
- TTL enforcement on lookup and write.
- Size enforcement after write, oldest-mtime first.
- Only owned `*.md` entries with the v3 envelope marker and owned temporary files may be deleted.
- Foreign files, provider health/stats, SQLite state and evidence-cache entries are never retention targets.
- Concurrent disappearance and permission errors degrade without crashing provider results.

## Additivity gate

All six Amendment-002 goldens MUST remain valid without modification. WS-2 fixtures live under `tests/fixtures/v3/ws2/` and are release-blocking.
