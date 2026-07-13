# Web Search Plus 3.0 Compatibility

## Public tools

The Hermes tool surface remains:

- `web_search_plus`
- `web_extract_plus`

Existing tool calls are projected into native `RequestV3` execution and returned through the legacy-compatible response projection. Callers do not need to construct v3 DTOs.

## Provider surface

WSP 3.0 registers 12 source-only search providers and 8 extraction providers. The generated [Provider Reference](PROVIDERS.md) is authoritative.

Native Perplexity and Kilo Perplexity are retained only as rejected registry records with `no_verified_source_only_endpoint`; they are not valid tool or CLI provider choices. This is an intentional charter correction, not a temporary outage.

## Routing

Classic Routing v2 remains authoritative in 3.0.

- Config default: `routing.policy_mode = "classic"`
- Emergency override: `WSP_ROUTING_CLASSIC_ONLY=1`
- The environment override wins over config.
- Unknown policy values fail closed to Classic.
- Shadow metadata, when present, is observational and must report `affected_execution=false`.

A full persisted shadow observer is deferred to 3.1.

## Cache

- v3 response entries are marker-owned and use the frozen 3.0 contract.
- Valid legacy cache entries can be read as `source_contract_version="2.x"`.
- Legacy answer/synthesis fields are discarded rather than promoted into v3 results.
- Cache clear and retention operations target only marker-owned entries; foreign or shared state files are preserved.
- Long extracted text remains page-on-demand under marker-owned `web/v3` storage.

## Operational state

WSP 3.0 uses SQLite for v3 circuit, budget, credential-slot, imported provider-health, and adaptive-sample state. Legacy `provider_health.json` and `provider_stats.json` can be imported together with the dry-run-first migration command.

SQLite failure degrades the operational state path; it must not silently bypass known auth, quota, config, or provider-contract blocks. Migration never deletes or rewrites the legacy JSON sources.

## Source-only response rules

Provider adapters must return capability-specific source envelopes:

- search: provider, query, and a list of source result objects;
- extract: provider and a list of URL result objects;
- non-empty synthesized `answer` values are rejected;
- provider identity and adapter signatures are checked against the registry.

Malformed provider envelopes become typed provider-contract failures and cannot enter the evidence spine.

## Python and deployment

- Python 3.10+ is required by the native v3 modules.
- Runtime code remains standard-library only.
- Plugin configuration and provider environment variables keep their existing locations.
- The Operator Console is local-only and read-only; it is not a hosted or remote management surface.
