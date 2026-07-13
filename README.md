# Web Search Plus — Hermes Plugin

<p align="center">
  <img src="docs/assets/web-search-plus-v3-hero.jpg" alt="Web Search Plus: source-only web intelligence for agents with provider-independent search, extraction, routing, and evidence receipts" width="100%">
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-22d3ee.svg"></a>
  <img alt="Python 3.10+" src="https://img.shields.io/badge/python-3.10%2B-34d399.svg">
  <img alt="Hermes Plugin" src="https://img.shields.io/badge/Hermes-plugin-a78bfa.svg">
</p>

**Web Search Plus is the source-only web layer for Hermes: one search tool, one extraction tool, and a provider mesh with conservative routing and honest execution metadata.** It searches and extracts across the providers you configure, without locking you into a single API or adding an answer-synthesis layer. Web Search Plus gathers and structures sources; it does not synthesize or verify truth.

It adds two Hermes tools:

- `web_search_plus` — routed multi-provider search with quality diagnostics
- `web_extract_plus` — clean URL extraction through provider backends

> Ported from [web-search-plus-plugin](https://github.com/robbyczgw-cla/web-search-plus-plugin) for the [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin API.

---

## What 3.0 improves

3.0 keeps the same two public tools and call style, but makes the evidence underneath them far more transparent and robust.

- **Source-only by construction.** Search and extraction stay separate from answer synthesis, claim generation, and truth verification. Provider endpoints without a verified source-only mode are rejected rather than quietly reshaped.
- **Traceable provenance.** Results point back to source observations, with typed records of providers tried, retried, skipped, failed, or served from cache.
- **Bounded extraction context.** Long pages return a useful preview while keeping the full cleaned text available on demand instead of flooding the agent context.
- **Conservative, explainable routing.** Classic Routing v2 remains authoritative, tie-breaking stays deterministic, and Brave joins the default auto-pool for independent-index diversity.
- **Honest failures.** Typed errors and response metadata expose missing credentials, rate limits, timeouts, empty results, and filters a provider could not apply.
- **Reversible upgrades.** State migration starts with a dry run, creates verified backups before writing, and supports rollback.
- **Local operational visibility.** The read-only Operator Console shows routing receipts, provider readiness, cache state, and applied bounds without calling providers or changing configuration.

Read the full [3.0 Release Notes](docs/RELEASE_NOTES_V3.md) for compatibility details and deliberate 3.1 deferrals.

---

## Quick Start

```bash
# 1) Install and enable the plugin
hermes plugins install robbyczgw-cla/hermes-web-search-plus --enable

# 2) Inspect provider readiness and configure the providers you use
python ~/.hermes/plugins/web-search-plus/setup.py status
python ~/.hermes/plugins/web-search-plus/setup.py setup --preset starter

# 3) Reload Hermes so the tools are registered
# CLI: exit and start `hermes` again, or use /reset in-session
# Gateway: /restart, then /reset

# 4) Optional shell smoke test
cd ~/.hermes/plugins/web-search-plus
python3 search.py --query "Hermes Agent latest release" --provider auto --quality-report
```

Add at least one search-capable provider for `web_search_plus`; add an extraction-capable provider for `web_extract_plus`. The setup helper stores keys in the active Hermes environment file — never commit them to the repository.

Update later with:

```bash
hermes plugins update web-search-plus
```

Python 3.10+ is required. Runtime code is standard-library only.

---

## The two tools

### `web_search_plus`

Use it for current information, source discovery, or opt-in multi-provider research.

```python
web_search_plus(query="Hermes Agent latest release")
web_search_plus(query="compare recent open-source agent frameworks", mode="research", quality_report=True)
```

### `web_extract_plus`

Use it when you already have URLs and want clean page content.

```python
web_extract_plus(urls=["https://example.com"])
web_extract_plus(urls=["https://docs.example.com"], provider="linkup", render_js=False)
```

Full parameters, freshness and locale behavior, provider selection, extraction controls, and cache management live in the [User Guide](docs/USER_GUIDE.md).

---

## Documentation

### Start & upgrade

- [User Guide](docs/USER_GUIDE.md) — installation, first-run checks, tool usage, and troubleshooting
- [3.0 Release Notes](docs/RELEASE_NOTES_V3.md) — highlights, provider changes, compatibility, and 3.1 deferrals
- [3.0 Migration](docs/V3_MIGRATION.md) — dry run, apply, smoke tests, and rollback
- [3.0 Compatibility](docs/V3_COMPATIBILITY.md) and [Backup & Restore](docs/V3_BACKUP_RESTORE.md) — stable surfaces and recovery behavior

### Configure & operate

- [Provider Reference](docs/PROVIDERS.md) — generated capabilities, environment variables, defaults, and signup links
- [Routing v2 Reference](docs/ROUTING.md) — generated routing classes, preferences, and demotions
- [Operator Console](docs/V3_OPERATOR_CONSOLE.md) — local read-only visibility and troubleshooting
- [Provider Benchmarks](docs/V3_BENCHMARKS.md) — search and extraction comparison with privacy and quota guidance
- [FAQ](docs/FAQ.md) — provider selection, cache, cost, and common setup problems

### Deep reference

- [Architecture](docs/ARCHITECTURE.md) — plugin boundaries, routing, cache/state flow, and provider extensions
- [3.0 Wire Contract](docs/V3_WIRE_CONTRACT.md) — normative request and response surface
- [Source Evidence](docs/V3_SOURCE_EVIDENCE_CONTRACT.md), [Bounded Context](docs/V3_BOUNDED_CONTEXT_CONTRACT.md), and [Search Parity](docs/V3_SEARCH_PARITY_CONTRACT.md) — detailed v3 contract amendments

---

## Development

```bash
cd ~/.hermes/plugins/web-search-plus
python3 -m pip install -r requirements.txt
python3 -m pytest -q
python3 -m compileall -q __init__.py search.py setup.py scripts tests
```

Generated provider and routing references are checked in CI. Development architecture and extension notes are in [Architecture](docs/ARCHITECTURE.md).

---

## License

MIT — see [LICENSE](LICENSE).

## Related

- [web-search-plus-plugin](https://github.com/robbyczgw-cla/web-search-plus-plugin) — TypeScript/OpenClaw version
- [Hermes Agent](https://github.com/NousResearch/hermes-agent) — the agent runtime this plugin extends
