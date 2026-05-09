# Changelog

## [v1.8.0] — 2026-05-09

### ✨ Added
- **`web_answer_plus`** — a new answer-first Hermes tool. It searches the web, selects useful sources, extracts the best pages when possible, and returns a concise answer with citations, warnings, freshness, confidence, and bounded-cost metadata.
- **Standalone provider setup** — `setup.py` now gives users a secret-safe way to inspect and configure provider keys without waiting for Hermes core plugin-CLI support.
- **Provider setup presets** — default setup walks through every supported provider; optional presets keep quick starts short (`starter`, `lean`, `search`, `extract`, `all`).
- **One-shot onboarding hint** — users with no configured provider keys get a single helpful setup hint instead of a dead tool surface.
- **README hero and release docs** — refreshed public documentation around the three main jobs: search, answer, and extract.

### 🔧 Improved
- Provider keys are now explained by capability, not as one fake “required key” list:
  - search-capable keys unlock `web_search_plus` and snippet-backed `web_answer_plus`;
  - extraction-capable keys unlock `web_extract_plus` and fuller cited answers.
- `web_answer_plus` keeps defaults cheap and predictable: quick mode uses 3 sources and up to 2 extracts; deep mode broadens search but still caps extraction at 5 URLs.
- Linkup is preferred for answer extraction, but it is not a hard dependency. If another extraction provider is configured, the normal extraction fallback path can still be used.
- If no extraction provider exists, `web_answer_plus` returns snippet-backed answers with an explicit warning instead of pretending it has full source text.
- Setup now respects `--env-path` consistently for both the dashboard and writes, preserving existing `.env` entries and never printing entered secret values.

### 🧪 Tests
- Added regression coverage for answer defaults, freshness detection, citation normalization, locale hints, output shapes, quick/deep mode selection, fallback extractor cost metadata, extraction status, cost guards, provider catalog, full-provider default setup, optional presets, target-env dashboard behavior, dry-run setup behavior, empty-key tool gating, and onboarding hints.
- Test suite: 81/81 unit tests passing locally.

## [v1.7.1] — 2026-05-06

### 🐛 Fixed
- Brave Search no longer fails on gzip-compressed API responses returned by `urllib.request.urlopen()`.
- Shared HTTP response parsing now handles `gzip`/`x-gzip`, gzip magic bytes, and `deflate` bodies for both GET and POST provider requests, including HTTP error bodies.

### 🧪 Tests
- Added regression coverage for gzip/deflate response decoding and Brave GET parsing through the shared urllib client.

## [v1.7.0] — 2026-05-03

### ✨ Added
- **Quality reports** for `web_search_plus` — optional diagnostics covering routing decisions, provider behavior, result counts, and quality metadata.
- **Research mode** — opt-in `mode="research"` path for multi-provider discovery plus selected URL extraction.
- **Golden query evaluator** — repeatable evaluation script and tests for tracking provider/research behavior over representative queries.

### 🔧 Improved
- Research mode now has a best-effort `research_time_budget` defaulting to 55 seconds, exposed through the Hermes tool schema and CLI as `--research-time-budget`.
- Extraction failures no longer fail the entire research response; partial search results are preserved and errors are reported in routing metadata.
- Budget exhaustion now skips remaining provider/extraction work instead of hanging or spending API calls blindly.
- Plugin metadata now matches the shipped tool surface: search, extraction, quality reports, and research mode.

### 🧰 Maintenance
- Added `requirements.txt` with bounded runtime dependencies.
- Added GitHub Actions CI for Ruff, pytest, and Python compile checks.
- Synchronized README, manifest, module headers, and CLI docs for the v1.7.0 release.

### 🧪 Tests
- Added regression coverage for research-mode extraction failures and time-budget exhaustion.
- Test suite: 47/47 unit tests passing.

### 🙏 Contributors
- Robby / **@robbyczgw-cla**

## [v1.6.1] — 2026-04-29

### 🔧 Improved
- **Shared retry path for provider execution** — extraction now uses the same transient-error retry behavior as search, reducing duplicated logic and making retry handling more predictable across providers.
- **Cooldown-aware extraction fallback** — `web_extract_plus` now skips providers already in cooldown and records those skips in routing metadata for clearer diagnostics.
- **Provider health reset on successful fallback** — successful extraction fallbacks now clear health state for the provider that ultimately succeeds.

### 🐛 Fixed
- Extraction fallback now records provider failure cooldown metadata when a provider exhausts retries and fails.
- Transient extraction failures (for example HTTP 503 / temporary upstream outages) now retry before failing over to the next provider.

### 🧪 Tests
- Added extraction tests for transient retry behavior, cooldown skipping, and provider health reset after fallback success.
- Test suite remains green: 35/35 unit tests passing.

### 🙏 Contributors
- Thanks **@Wysie** for the implementation behind this release (`refactor extract plus resilience reuse`, PR #7).

## [v1.6.0] — 2026-04-25

### ✨ Added
- **web_extract_plus** — companion tool to web_search_plus for URL content extraction via Firecrawl, Linkup, Tavily, Exa, and You.com. Unified result shape, per-URL error handling, automatic provider fallback. Use cases: clean markdown from a page, structured content for downstream LLM processing, multi-provider redundancy.
- New CLI flags: --extract-urls, --format html|markdown, --extract-images, --include-raw-html, --render-js
- Image extraction support — Firecrawl, Linkup, and Tavily can return image metadata via include_images=True

### 🔧 Improved
- Auto-fallback now triggers when primary provider returns all-URL errors (previously stopped at first non-empty results array)
- Response includes requested_provider field for transparency when fallback kicks in
- web_extract_plus only registers when an extraction-capable provider is configured (Firecrawl/Linkup/Tavily/Exa/You) — no more dead tool with search-only keys

### 🐛 Fixed
- Firecrawl include_images was a silent no-op; now parses markdown image syntax + ogImage metadata
- Invalid URLs (no http/https scheme) returned through the entire fallback chain unnecessarily; now return clean validation error
- Empty --extract-urls crashed argparse; now returns clean JSON error

### 🧪 Tests
- 9 → 15 unit tests; full coverage of new behavior (fallback cascade, check_fn scoping, image parsing, error paths)

### 🙏 Contributors
Thanks @Wysieie for the implementation.

## [1.5.0] - 2026-04-24

### Added
- **Linkup provider** — source-grounded search with citations and fact-check signals. New regex dict `LINKUP_SOURCE_SIGNALS` (6 groups), bearer auth, parses both sourced-answer and standard search results.
- **Firecrawl provider** — web search with scrape-ready structured content. Scoring: `discovery_score + research_score * 0.35 + recency_score * 0.25`.
- Helper `load-env-file` supports plugin-local and legacy parent `.env` paths.

### Changed
- Provider priority order: tavily → linkup → querit → exa → firecrawl → perplexity → brave → serper → you → searxng.

### Credits
- Thanks @wysiecla for the contribution!

All notable changes to the Hermes web-search-plus plugin are documented here.

---

## [1.4.0] — 2026-04-23

### Added
- **Brave Search provider** — new independent search index with generous free tier (2000 queries/month). Huge thanks to **@Wysie** for the full implementation (#4). Reduces reliance on Serper/Tavily and adds a strong fallback when Google-backed providers rate-limit.
- `BRAVE_API_KEY` env support + `.env.template` entry + README provider matrix update (also @Wysie)
- `tests/test_tie_breaker.py` — unit coverage for the SHA-256 deterministic tie-breaker (`_choose_tie_winner`): single-winner passthrough, same-query stability, distribution fairness across 200 queries, fallback without priority list

### Fixed
- Hermes `main` branch compatibility: plugin now survives the updated toolset resolution in Hermes core (thanks again **@Wysie**, #4)

### Contributors
- **@Wysie** — Brave provider + Hermes main compat (PR #4). Second merged PR from Wysie after the virtualenv docs fix in 1.3.1. Top external contributor 🏆

---

## [1.3.1] — 2026-04-23

### Fixed
- Plugin `.env` file now loads on module import, ensuring API keys are available at tool registration time (thanks @josh-clarke, #1)
- `plugin.yaml` metadata: corrected `requires_env` schema and Hermes repo link

### Added
- MIT license file
- README: Quick Start section, routing transparency, adaptive fallback explanation
- Docs: Hermes virtualenv setup clarification to prevent dependency-install-in-wrong-env footgun (thanks @Wysie, #3)

---

## [1.3.0] — 2026-03-17

### Added
- `time_range` parameter: filter results by recency (`day`, `week`, `month`, `year`)
- `include_domains` parameter: whitelist specific domains (e.g. `["arxiv.org"]`)
- `exclude_domains` parameter: blacklist specific domains (e.g. `["reddit.com"]`)
- `you` added to provider enum (was missing from schema)
- Feature parity table in README

### Changed
- Timeout increased from 65s to **75s** (aligned with OpenClaw plugin)
- README: install guide, full parameter table, examples, architecture, feature parity table

### Notes
- Now fully feature-parity with [OpenClaw web-search-plus-plugin](https://github.com/robbyczgw-cla/web-search-plus-plugin) main branch

---

## [1.2.0] — 2026-03-17

### Added
- `depth` parameter for Exa deep research modes:
  - `deep`: multi-source synthesis (4-12s latency)
  - `deep-reasoning`: cross-document reasoning and analysis (12-50s latency)
- Timeout increased from 30s to 65s to support long-running deep-reasoning queries
- Full README with routing table, parameter docs, examples, architecture section
- CHANGELOG

### Fixed
- Handler now correctly unpacks input dict passed by Hermes registry
  (was causing "expected str, bytes or os.PathLike object, not dict" on all tool calls)
- `depth` parameter name aligned with OpenClaw plugin (was `exa_depth` in initial port)

### Notes
- Synced with [OpenClaw@908b145](https://github.com/robbyczgw-cla/web-search-plus-plugin/commit/908b14529230b1b300e44c6dd2cc8171833c1abb)

---

## [1.1.0] — 2026-03-17

### Fixed
- Plugin handler dict-unpacking bug: Hermes registry passes full input dict as first
  positional argument, not keyword args. Added `isinstance(args_or_query, dict)` check.

---

## [1.0.0] — 2026-03-17

### Added
- Initial Hermes plugin port of web-search-plus from OpenClaw TypeScript plugin
- Auto-routing across Serper, Tavily, Exa, Querit, Perplexity, SearXNG
- `provider` parameter to force a specific provider
- `count` parameter for result count (1-20)
- Hermes plugin registration via `register(ctx)` in `__init__.py`
