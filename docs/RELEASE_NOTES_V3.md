# Web Search Plus 3.0 Release Notes

WSP 3.0 turns the plugin into a source-only evidence engine with an operator-owned local control plane. Public tool names remain stable; execution now records where evidence came from, what was attempted, what was filtered, and what was served from cache without pretending that the plugin generated or verified truth.

## Highlights

- **Native v3 execution:** frozen `RequestV3`/`ResponseV3` contracts with typed attempts, errors, routing receipts, observations, policy actions, cache status, and applied limits.
- **Source-only charter:** search and extraction only. Provider answer synthesis, claim generation, and verification judgments are rejected at request and adapter boundaries.
- **Lossless evidence spine:** provider observations are recorded before deduplication, filtering, reranking, or truncation; result provenance points back to source observations.
- **Bounded context:** large extraction results stay usable through deterministic truncation and marker-owned page-on-demand full-text storage.
- **SQLite operational state:** circuit, budget, credential-slot, imported health, and adaptive sample state share one transactional store with graceful degradation.
- **Reversible migration:** dry-run-first import of `provider_health.json` and `provider_stats.json`, verified backups, idempotent apply, and digest-checked rollback.
- **Provider adapter protocol:** registry coverage, exact callable signatures, provider identity, source-only result envelopes, and typed contract failures are enforced.
- **Operator Console:** local read-only UI over sanitized overview, receipt, and benchmark snapshots; loopback-only with startup-token authentication.
- **Extraction benchmark:** real direct-provider comparison with aggregate privacy-safe history and explicit recommendations.
- **Emergency routing control:** Classic Routing v2 remains authoritative; config and environment kill switches fail closed to Classic.

## Provider changes

WSP 3.0 exposes 12 source-only search providers and 8 extraction providers. Native Perplexity and Kilo Perplexity answer endpoints are no longer registered because no verified source-only endpoint is available. Existing `PERPLEXITY_API_KEY` and `KILOCODE_API_KEY` values are ignored rather than deleted, and those provider IDs are rejected for search and extraction.

## Compatibility

- Hermes tools remain `web_search_plus` and `web_extract_plus`.
- Existing tool arguments continue through the legacy-to-v3 compatibility projection.
- Valid legacy cache source results can be read; synthesis fields are dropped.
- Existing provider keys and behavior config remain in place.
- Search and extraction priority lists remain independent.

See [3.0 Compatibility](V3_COMPATIBILITY.md) and [Migration](V3_MIGRATION.md) before upgrading.

## Operator actions

1. Back up the installed plugin and config.
2. Run `python3 search.py state-migrate`.
3. Apply with `python3 search.py state-migrate --apply` and retain the backup ID.
4. Run doctor plus search/extraction smoke tests.
5. Start the Console with `python3 ui.py --port 8765` if desired.

## Deliberately deferred to 3.1

- full persisted shadow-observer policy evaluation;
- self-hosted/no-paid-key operating profile;
- hosted, remote, account, or write-capable Console surfaces.

WSP 3.0 does not claim those features.
