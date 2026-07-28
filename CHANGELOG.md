# Changelog

## [Unreleased]

### 📚 Docs
- Added a repository-specific contribution guide covering local setup, the source-only provider contract, SDK-based provider intake, CI and generated-artifact gates, changelog hygiene, security reporting, and maintainer-only release boundaries.

## [v3.3.0] — 2026-07-24

### ✨ Added
- Bounded heading-aware semantic spans: a query-relevant ATX Markdown heading now retains its own section, including deeper subheadings and query-free body text, through the next same-or-shallower heading. Selection remains deterministic and offset-safe, with a two-section cap and a hard 1,200-codepoint cap per heading section.
- Research Mode now harvests provider completions as they arrive and can stop waiting once a conservative, configurable quality quorum has been reached. Public result and diagnostic order remain deterministic, while preempted providers are reported explicitly as `preempted_after_quorum` rather than disappearing.

### ✨ v3.1 result enrichment
- Added additive, provenance-safe cross-provider snippet aggregation for canonical-URL clusters. Every retained snippet fragment names its `observation_id` and `source_field`; the wire validator reconstructs aggregate text from the validated fragments and explicit separator. Identical/contained fragments are deterministically deduplicated and aggregate previews stop at 600 characters without losing fragment attribution.
- Added provider-neutral heuristic `source_type` (`value`, `method`, `method_version`, `confidence`) plus explainable per-result `fetch_priority` tiers with closed reason codes derived from cluster consensus, rank, and source-type authority. These fields are structured hints, not truth or quality claims.
- Aligned Hound search with extraction by preserving Hound's `source_type` signal for WSP's normalized heuristic projection; `fetch_relevance` and `engines_consensus` remain available as adapter evidence.

### 🐛 Fixed
- Explicit `--research-providers` now bypass the automatic-routing allowlist, matching explicit single-provider semantics while still honoring disabled, unconfigured, and cooldown safety gates. Cooldown omissions remain visible in both routing and quality receipts instead of silently disappearing.
- Research quorum evidence now scans all unique candidates from completed providers instead of stopping after the first provider fills the public result target. Later providers can therefore satisfy the provider-diversity requirement while the returned result page remains capped normally.

### Credits
- The independently implemented heading-aware interaction is inspired by [Hound/Master-Fetch v11.2.0](https://github.com/dondai1234/master-fetch), the independent MIT project by [Bishesh Bhandari (`dondai1234`)](https://github.com/dondai1234). This recognizes respectful upstream collaboration; WSP does not import, fork, or copy Hound/Master-Fetch code.
- Hound/Master-Fetch is an independent MIT project by Bishesh Bhandari ([`dondai1234`](https://github.com/dondai1234)), https://github.com/dondai1234/master-fetch. WSP ports and adapts the integration idea through its own adapter; this is not Robby's Hound code and does not bundle, fork, or claim ownership of Hound.
- The completion-order and quality-quorum design adapts ideas from [Hound/Master-Fetch v11.2.0](https://github.com/dondai1234/master-fetch/releases/tag/v11.2.0), the independent MIT-licensed project by [Bishesh Bhandari (`dondai1234`)](https://github.com/dondai1234). The WSP implementation was reworked for its own provider, budget, provenance, and receipt contracts.

## [v3.2.0] — 2026-07-22

### ✨ Added
- Added Hound as an optional local MCP provider for both source search and URL extraction. Hound remains explicit-only by default, and WSP connects to a separately installed loopback sidecar instead of bundling or importing Hound internals.
- Added a dedicated Hound operator guide covering separate installation, loopback-only service configuration, explicit Search/Extract verification, keyless trade-offs, privacy boundaries, caching ownership, and deliberate auto-routing opt-in.

### 🔒 Security and reliability
- Restricted Hound endpoints to uncredentialed HTTP on `127.0.0.1` or `::1`; remote hosts, hostnames, URL userinfo, query strings, fragments, redirects, proxy-environment routing, and oversized MCP responses fail closed.
- Project Hound transport, timeout, MCP, and malformed-payload failures into typed provider errors while preserving requested extraction cardinality and never promoting missing upstream content to success.
- Disabled Hound's per-request cache through the adapter so WSP remains the authoritative routing, freshness, evidence-cache, and receipt layer.
- Applied `auto_allow=false` consistently to search and extraction fallback, keeping Hound out of automatic traffic unless an operator opts in.
- Aligned provider-benchmark eligibility with the same auto-allow gate, preventing configured explicit-only providers from being recommended for automatic priority before operator opt-in.

### Credits
- Hound is an independent MIT-licensed project created and maintained by [Bishesh Bhandari (`dondai1234`)](https://github.com/dondai1234). WSP 3.2 ships only its own MCP client adapter; Hound is installed and operated separately.
- Thanks to the Hound project for the open MCP interface and constructive upstream collaboration around domain validation, canonical URL normalization, and content classification. See [3.2 Release Notes](docs/RELEASE_NOTES_V32.md) for the linked upstream work.

## [v3.1.2] — 2026-07-21

### 🐛 Fixed
- Extraction requests served by discovered Provider-SDK extraction providers no longer fail closed inside the cache identity. SDK providers now contribute a deterministic identity derived from their spec and the non-secret scalar settings of their config section (credential-shaped keys are excluded, because the identity is persisted with cached evidence). Unknown, unregistered providers still fail closed.
- The `providers.d` non-production gate now acts before module execution: modules declaring a literal `production=False` are skipped without being imported unless `WSP_SDK_ALLOW_NON_PRODUCTION` is set. Previously such modules were excluded from the registry but their module-level code still ran. The post-import gate remains as the authoritative backstop for dynamically computed flags.

## [v3.1.1] — 2026-07-20

### 🐛 Fixed
- Refresh Operator Privacy provider provenance from the validated live registry so providers registered after the privacy module was imported can emit receipts without weakening the fail-closed rejection of unregistered provider IDs.
- Isolate the opt-in Provider SDK fixture probe in a temporary cache root, preventing test receipts and cache entries from touching an operator's active WSP state.
- Bring the contract schema generator back into parity with the 3.1 budget-preflight schema, so the generated-artifact CI gate no longer treats the published response schema as stale.
- Stabilize receipt-journal concurrency tests with the journal's injected clock, removing wall-clock expiry from thread and cross-process retention assertions.

## [v3.1.0] — 2026-07-20

### ✨ Added
- Added the `self_hosted` no-paid-key operating profile. Its runtime-derived auto pools use SearXNG and keyless Keenable, preserve explicit keyed overrides with visible result metadata, and expose offline profile prerequisites through `setup.py status`.
- Added the additive-only WSP 3.x public Provider SDK (`wsp_sdk`) with automatic `providers.d` discovery, typed startup diagnostics, fail-closed duplicate IDs, shared provider conformance checks, and `setup.py new-provider` scaffolding. New provider modules supply their own formal adapters without core registry or dispatch edits; discovered providers remain explicit-only unless they explicitly opt into the existing auto-routing gate.
- Added persisted, deterministic Shadow quality-policy observations for auto-routed searches. Classic Routing v2 remains authoritative; the new local Operator Console aggregate reports agreement and provider divergences without storing query text.
- Added opt-in v3 budget preflight. Provider fan-out, daily ledger quota, request deadline, and extraction context are checked before provider execution, with typed receipt evidence for deterministic degradation or zero-attempt budget failures.
- Added deterministic Diversity Score diagnostics for quality reports: registrable-domain coverage, canonical-URL duplication, near-duplicate snippets, and research-provider mix. Research-result reranking remains explicitly opt-in.
- Added the versioned v3 extraction-cache identity contract: request-exact URL, budget, bounded-context, extraction-control, provider-endpoint, URL-policy and retained-storage variation; lossless extraction provenance/legacy alias round-trips; and fail-closed identity-version and corrupt-entry quarantine handling.
- Added opt-in semantic span extraction on `web_extract_plus` (`spans`/`spans_query`): deterministic query-conditioned passage selection over the NFC-normalized cleaned text with a mechanical offset contract — Unicode codepoint indices, half-open `[start,end)`, slicing invariant, and `within_preview` flags valid against the retained full text (docs/V3_SPAN_CONTRACT.md).
- Added the read-only Operator Console endpoint `/api/v3/provider-health`: per-provider daily trend buckets (samples, errors, error rate, result counts, median latency) aggregated from persisted adaptive samples, without provider calls or stored query text.

### ⚠️ Deprecated
- The legacy pre-v3 execution modules (`cache.py` search-response caching and the non-v3 projection paths they serve) are deprecated. All public tools already execute through the native v3 orchestrator; the legacy modules remain only as compatibility shims and are planned for removal no earlier than 3.2. Operators do not need to change anything — this is an advance notice, not a behavior change.

## [v3.0.2] — 2026-07-14

### Credits
- #104 by @robbyczgw-cla — repaired and hardened the native v3 Research, Extract cache, bounded-context, retained full-text, and operator-receipt integration paths.

### 🐛 Fixed
- Restored true multi-provider Research Mode in the native v3/Hermes path. Research providers now execute as separate authoritative attempts, and source observations retain the provider-attempt provenance of each contributing backend.
- Restored the complete public Research envelope and its single post-merge quality pass, bypassed the legacy lossy cache, classified started deadline overruns as cancelled attempts, degraded provider/extraction budget limits truthfully, and marked total fan-out failure as a failed response.
- Preserved extracted page content, safe `raw_content` aliases, and per-result provider attribution on v3 cache hits instead of projecting extraction evidence as search-only snippets.
- Keyed extraction cache entries on the complete requested URL list, attempt budget, effective context limits, and current URL/storage policy, while still enforcing the provider fan-out cap before execution. Lossy partial-error, raw-HTML, image, and provider-specific payloads now bypass cache writes.
- Made retained full-text references content-versioned and revalidated them on cache hits, preventing a refresh of the same URL from silently changing older cached evidence.
- Applied the global extraction context budget before v3 cache writes, operator receipts, and legacy Hermes projection, so cache misses and hits share the same deterministic fair-share output without writing requested URLs into operator receipts.

### 📚 Docs
- Removed stale Perplexity/Kilo credential and freshness claims from active plugin metadata and replaced obsolete provider-toggle examples.

## [v3.0.1] — 2026-07-13

### 🐛 Fixed
- Fixed in-process engine loading when Hermes' host package root precedes an already-registered plugin path. The lazy loader now temporarily prioritizes Web Search Plus sibling modules and restores the exact original `sys.path`, preventing collisions with Hermes' `providers` package and avoiding a permanent subprocess fallback.

## [v3.0.0] — 2026-07-13

### 🚀 WSP 3.0 — Source-only evidence engine
- Added the native source-only v3 evidence spine: frozen request/response contracts, lossless provider observations, complete attempt receipts, policy actions, cache-origin evidence, bounded context, typed errors, and marker-owned operational storage.
- Added the local read-only Operator Console with loopback-only binding, startup-token authentication, privacy-filtered overview/receipt/benchmark APIs, and a runnable `python3 ui.py --port 8765` entrypoint.
- Added dry-run-first legacy-state migration for `provider_health.json` plus `provider_stats.json`, with verified backups, transactional import, idempotent apply, and digest-checked rollback.
- Added a formal provider-adapter protocol that fails closed on registry/signature drift, provider identity mismatch, malformed result envelopes, and non-source answer payloads.
- Added direct extraction benchmarking with privacy-safe aggregate Console history and independent extraction-priority recommendations.
- Added a two-level Classic-routing kill switch: `routing.policy_mode` plus the higher-priority `WSP_ROUTING_CLASSIC_ONLY=1` environment override.

### 💥 Changed in 3.0
- WSP is now mechanically source-only. Native Perplexity and Kilo Perplexity answer endpoints are no longer registered because they do not expose a verified source-only mode; the public surface is 12 search providers and 8 extraction providers.
- Classic Routing v2 remains authoritative in 3.0. Full persisted shadow-observer evaluation and the self-hosted/no-paid-key profile are explicitly deferred to 3.1.
- The Operator Console default state path now matches the engine's `v3/state.sqlite3` layout.
- Brave Search is promoted to the default Routing v2 auto-pool for independent-index source diversity; its free-tier quota and rate limits use the same existing provider cooldown and fallback handling as the other free-tier auto providers.

### 🎯 What 3.0 improves in practice
- Every result is easier to audit: typed provenance connects it to the underlying source observation, while provider attempts, retries, skips, and cache origin remain visible.
- Long pages no longer need to flood agent context: extraction returns a bounded preview and keeps the full cleaned text available on demand.
- Provider failures become legible: missing credentials, rate limits, timeouts, empty results, and unapplied filters are represented explicitly instead of disappearing into silent gaps.
- Upgrades are safer to try: state migration starts with a dry run, backs up existing state before writing, and supports digest-verified rollback.
- Routing gains independent-index diversity without a surprise policy switch: Classic Routing v2 remains authoritative while Brave joins the default auto-pool.
- Operators gain local, read-only visibility into routing receipts, provider readiness, cache state, and applied limits without triggering provider calls or configuration writes.

### ✨ Added before 3.0, carried forward
- Added independent `auto_routing.extract_provider_priority` configuration for `web_extract_plus(provider="auto")`, with `setup.py config set-extract-priority ...`. The existing Tavily-first registry order remains the public default; partial lists append missing extract-capable providers, and search `provider_priority` remains independent.

### 📚 Docs
- Added 3.0 migration, compatibility, backup/restore, Operator Console, benchmark, and release-note guides.

## [v2.9.1] — 2026-07-10

### Credits
- #82 by @robbyczgw-cla — synchronized the last stale v2.9.0 User-Agent version surface.
- #83 by @robbyczgw-cla — added the atomic release-version preparation helper and regression tests.
- #84 by @robbyczgw-cla — aligned Serper extraction documentation and streamlined the README.
- #86 by @robbyczgw-cla — raised Parallel extraction content budgets for fair long-page handling.
- v2.9.1 maintenance by @robbyczgw-cla — protected shared provider/usage state from cache stats and clear operations.

### 🔧 Improved
- Raised Parallel extraction's default `full_content` budget to 60k characters per result / 120k total so long pages are evaluated fairly against other extraction providers instead of being silently capped at 6k. Operators that need smaller Parallel payloads can set `parallel.max_chars_per_result` and `parallel.max_chars_total` in `config.json`. (#86)
- Added `scripts/prepare_release.py`: bumps every release-version surface in one step (plugin.yaml, `__version__`, header docstrings, User-Agent, the test gate, and the CHANGELOG section) with a dry-run default and loud failure on surface drift. The hardcoded release gate now lives in exactly one place (`tests/test_release_metadata.py`), while package-import and HTTP-client tests read the expected version dynamically from `plugin.yaml`. (#83)

### 🐛 Fixed
- Synchronized the default HTTP User-Agent with v2.9.0, closing the last stale release-version surface left after that tag. (#82)
- Cache statistics and clearing now recognize only complete WSP search-cache envelopes. Shared state such as `provider_stats.json`, `provider_health.json`, host-written `usage_events.json`, unrelated JSON, and corrupt foreign files is ignored and preserved byte-for-byte instead of being counted, deleted, or crashing cache stats.

### 📚 Docs
- Marked Serper extraction consistently across the README, architecture, and user guide, and slimmed the README into a clearer product landing page. (#84)

## [v2.9.0] — 2026-07-03

### Credits
- #75 by @robbyczgw-cla — cwd-independent plugin import fix for Hermes standalone discovery.
- #77 by @robbyczgw-cla — golden snapshot recorder and expanded snapshot suite.
- #79 by @robbyczgw-cla — registry-driven provider dispatch (separates routing from provider execution).
- #80 by @robbyczgw-cla — Serper news endpoint and webpage scraper extraction.
- #81 by @robbyczgw-cla — configurable search locale defaults with lightweight query language detection.

### ✨ Added
- Golden snapshot evaluation recorder and expanded snapshot suite: record golden snapshots for regression testing, with expanded query coverage across providers. See `scripts/golden_eval.py`. (#77)
- Added a unified `search_type` parameter to `web_search_plus` (`search` or `news`). Serper serves the news vertical natively via `google.serper.dev/news` (the unified `freshness` filter keeps working there); all other providers run their normal search and report `search_type.applied=false` in result metadata, mirroring the `freshness` contract. CLI: `--search-type`.
- Serper is now an extraction provider: `web_extract_plus(provider="serper")` scrapes pages via Serper's webpage scraper (`https://scrape.serper.dev`, markdown preferred, per-URL error items). It joins the auto-extraction fallback chain in last position — Tavily-first ordering is unchanged. The endpoint is operator-overridable via config `serper.scrape_url` (with `serper.extract_timeout`).
- Configurable search locale defaults with lightweight query language detection. A new `defaults.locale` config section (`country`: ISO 3166-1 alpha-2, `language`: ISO 639-1 or `"auto"`) replaces the hardcoded us/en provider defaults for Serper, Brave, You.com, SerpBase, Querit, Firecrawl, and SearXNG. Resolution is config-first for the region and query-aware for the language: CLI/tool flags > explicit provider config > explicit location hint in the query (curated city/country table, e.g. "mejores restaurantes Madrid" → `es`) > `defaults.locale` > us/en fallback. With `language: "auto"`, a conservative stdlib stopword/character heuristic infers `de`, `es`, `fr`, `it`, `pt`, `nl`, or `en` (at least two distinct signals with a single unambiguous winner; terse technical queries like "PostgreSQL 17 release notes" keep the default). Query language never implies the country — a German query may come from Austria or Switzerland, so only explicit location hints move the region. Result metadata reports the resolved locale and per-value source (`config|hint|cli|fallback` / `config|inferred|cli|fallback`). Without `defaults.locale` and flags, behavior stays exactly us/en.

### 🔧 Improved
- Registry-driven provider dispatch: separates routing from provider execution. Provider-specific search/extract logic lives in `provider_dispatch.py` instead of being scattered through `routing.py`. (#79)

### 🐛 Fixed
- Plugin discovery no longer depends on the current working directory when Hermes loads the flat plugin from outside the plugin directory. (#75)
- `serper.type = "news"` (and the new `search_type="news"`) no longer returns silently empty results: Serper `/news` answers carry results under `news` instead of `organic`, and the parser now reads the right field, including `date`, `source`, thumbnail, and position metadata.

## [v2.8.1] — 2026-07-02

### Credits
- #75 by @robbyczgw-cla — cwd-independent plugin import fix for Hermes standalone discovery.

### 🐛 Fixed
- Web Search Plus plugin discovery no longer depends on the current working directory when Hermes loads the flat plugin from outside the plugin directory. The plugin root is added as a fallback import path so sibling modules such as `provider_registry` resolve under Hermes Agent v0.18 standalone discovery without shadowing host modules.

## [v2.8.0] — 2026-07-02

### Credits
- #65 by @robbyczgw-cla — truncate-and-store handling for large `web_extract_plus` pages.
- #66 by @robbyczgw-cla — provider/decode/read-timeout error classification.
- #67 by @robbyczgw-cla — `.env` and cache permission hardening plus tighter CI workflow defaults.
- #68 by @robbyczgw-cla — look-alike domain boost hardening.
- #69 by @robbyczgw-cla — generated provider reference and drift check.
- #70 by @robbyczgw-cla — generated Routing v2 reference and drift check.
- #71 by @robbyczgw-cla — unified `freshness` parameter for `web_search_plus`.
- #72 by @robbyczgw-cla — provider bench and `provider_priority` recommendation command.

### ✨ Added
- `web_extract_plus` now uses truncate-and-store output handling for large extracted pages: short pages are returned in full, while long pages return a head/tail window plus a page-on-demand footer pointing to the full cleaned text stored under `cache/web`. Configure the inline budget with `web.extract_char_limit` (default `15000`). (#65)
- Added a unified `freshness` parameter to `web_search_plus` (`day`, `week`, `month`, `year`). Providers with native date filters receive the mapped value; providers without support transparently report that freshness was not applied instead of pretending recency was enforced. (#71)
- Added a provider bakeoff command — `python3 search.py --bench` (or `search.py bench` / `setup.py bench`) — that runs a small fixed query suite (docs, vendor release, community, non-English) against every configured search provider in-process and reports success rate, median latency, result volume, and quality signals (duplicate-free URLs, snippet coverage). It prints a ranked `auto_routing.provider_priority` recommendation with the exact `config set-priority` command to apply it; config is never written automatically, and bench traffic never triggers provider cooldowns or feeds adaptive routing stats. (#72)
- Added generated Provider and Routing v2 reference docs, plus drift checks so the public docs stay aligned with the provider registry and routing configuration. (#69, #70)

### 🛡️ Security
- Domain boost matching now avoids granting authority boosts to look-alike domains that merely contain a trusted domain string (for example `example.com.evil.test`). (#68)
- Setup-created `.env` files are written with `0600` permissions, cache directories are created with `0700`, and the CI workflow uses tighter token permissions/concurrency defaults. (#67)

### 🔧 Improved
- Inline base64 image data in extracted Markdown is replaced with `[IMAGE: alt]` placeholders before measuring/storing content, preventing data-URI token bombs while preserving normal `http(s)` image links. (#65)
- Provider decode failures and Python 3.8/3.9 read-timeout behavior are classified as provider errors, improving retry/fallback behavior and error clarity. (#66)

## [v2.7.0] — 2026-06-30

### Credits
- #60 by @IlyaGusev — keyless public-tier setup flow for keyless providers.
- #61 by @robbyczgw-cla — private/internal extraction target URL guard.
- #62 by @robbyczgw-cla — public-Hermes fast-path advisory doctor.
- #63 by @robbyczgw-cla — prevent provider config errors from marking provider health cooldowns.
- #59 by @robbyczgw-cla — README hero refresh and Querit signup URL correction carried forward from the v2.6.1 post-release range.

### ✨ Added
- Added `setup.py fastpath`, a dependency-free advisory doctor that checks whether Web Search Plus is installed for direct Hermes tool registration and whether current public-Hermes config (`agent.disabled_toolsets: [web]`) is present for lower-latency routing without requiring Hermes core patches. (#62)
- The setup wizard now offers the keyless public tier for keyless providers (currently Keenable): skip the key prompt and it asks whether to enable the no-key public endpoint, writing `<provider>.allow_public: true` to `config.json`. Add `--keyless-public` to skip that confirmation prompt and opt in directly. The mechanism is driven by the registry's keyless flag, so it covers future keyless providers automatically. (#60)

### 🛡️ Security
- `web_extract_plus` now rejects private/internal extraction target URLs by default before provider dispatch, blocking loopback, RFC1918, CGNAT/shared-address ranges, IPv6 ULA/link-local/mapped-private addresses, multicast, cloud metadata, and hostnames that resolve to private IPs. Operator-configured provider endpoints (for example a local Firecrawl-compatible backend) remain allowed; trusted intranet extraction can be opted into with `extract.allow_private_urls: true` in `config.json`. (#61)

### 🐛 Fixed
- A routing-config rewrite (e.g. `config set-priority`, `config reset`) no longer drops non-routing provider sections from `config.json` (e.g. `keenable.allow_public`, `keenable.search_url`, `searxng.instance_url`); the writer now merges routing keys onto the existing file instead of rebuilding it from routing defaults. (#60)
- Provider configuration errors such as missing API keys no longer mark providers unhealthy or put them into cooldown. Cooldown now stays reserved for real provider/network failures. (#63)
- Corrected the Querit provider `signup_url` from the dead `querit.com` to `querit.ai`. (#59)

### 📚 Docs
- Documented the current public-Hermes fast-path config and the new `setup.py fastpath` checker for users who want lower perceived latency without local Hermes core patches. (#62)
- Refreshed the README hero graphic for v2.7.0 with the current 14 search / 7 extraction provider taxonomy. (#59)

## [v2.6.1] — 2026-06-26

### Credits
- #57 by @robbyczgw-cla — GroktoCrawl / local Firecrawl-compatible backend documentation and endpoint override tests.

### 📚 Docs
- Documented using Firecrawl-v2-compatible local backends such as GroktoCrawl by overriding the existing Firecrawl search and scrape URLs in `config.json`.
- Corrected the v2.6.0 changelog history to include #55 and #56 attribution after the GitHub Release notes were also fixed.

### 🧪 Tests
- Added Firecrawl provider tests covering custom search and scrape endpoint overrides so local-compatible backends stay on the same wire path as Firecrawl cloud.

## [v2.6.0] — 2026-06-26

### Credits
- #55 by @maksym-mishchenko — in-process loader fix for `sys.modules` name collisions with host packages.
- #56 by @IlyaGusev — Keenable search and extraction provider with keyed endpoints plus an opt-in keyless public tier.

### 🐛 Fixed
- Fixed in-process loading when the host runtime already has top-level modules such as `providers` in `sys.modules`, preventing host/package name collisions from forcing the plugin onto the subprocess fallback path. (#55)

### ✨ Added
- Added Keenable as a search and extraction provider, using Keenable's independent web index. Setting `KEENABLE_API_KEY` (or `keenable.api_key` in `config.json`) uses the authenticated endpoints (with an `X-API-Key` header). It can also run keyless against the `/v1/search/public` and `/v1/fetch/public` endpoints, but this is **opt-in and off by default** — enable it with `keenable.allow_public: true` in `config.json` or `KEENABLE_ALLOW_PUBLIC=1`, since the public tier routes queries and fetched URLs to an unauthenticated service (~1000 req/hour, 10 req/sec per-IP limits, no SLA) and emits a one-time warning when first used. Once configured (keyed or opted-in), Keenable is available via `provider="keenable"` and as the lowest-priority auto-routing/extraction fallback, so it never displaces a configured keyed provider. Key status stays truthful — keyless providers report `key=no` with a distinct keyless badge in `doctor`. (#56)

## [v2.5.1] — 2026-06-16

### 🐛 Fixed
- `extract_plus` now respects `disabled_providers` from `config.json`. Previously only search routing honored the disabled-provider list; extraction used a hardcoded provider order, causing disabled providers to still be called during URL extraction. Explicit provider selection still tries the requested provider first, matching search semantics.

### 🔧 Improved
- Added tests covering auto-mode extraction skip and explicit-provider fallback behavior when providers are disabled in config.

## [v2.5.0] — 2026-06-16

### Credits
- #51 by @robbyczgw-cla — research/extraction budget enforcement, bounded daemonized provider work, 429 handling, and cooldown decay.
- #53 by @robbyczgw-cla — adaptive routing performance memory plus spam/mirror result filtering; rebased continuation of #52 after the stacked base landed.

### ✨ Added
- Added provider performance memory so auto-routing can learn from recent provider latency/success behavior without polluting live operator state during tests. (#53)
- Added spam/mirror result filtering and domain diversity safeguards for cleaner search results. (#53)

### 🐛 Fixed
- Research mode and concurrent extraction now enforce remaining time budgets around submitted futures, preserving partial completed results instead of waiting behind slow providers. (#51)
- Provider HTTP handling now treats `429 Retry-After` separately from generic transient failures and caps inline waiting so rate limits become cooldown metadata rather than user-visible hangs. (#51)
- Provider cooldown escalation now decays stale failure history instead of punishing isolated old failures forever. (#51)

### 🔧 Improved
- Result filtering now matches blocked domains only by exact domain or true subdomain, avoiding lookalike false positives such as `blocked.example.evil.test`. (#53)
- Explicit domain intent (`site:` queries and `include_domains`) now bypasses default diversity/spam reranking so user constraints win. (#53)

### 🧪 Tests
- Added process-exit, daemon task, budget timeout, rate-limit, provider health decay, provider stats, and result-quality filter regression coverage. (#51, #53)

## [v2.4.0] — 2026-06-08

### Credits
- #50 by @robbyczgw-cla — in-process `web_search_plus`/`web_extract_plus` execution and parallel research mode.
- #46 by @robbyczgw-cla — Hermes profile `.env` loading for provider keys.
- #49 by @wysie — plugin update instructions in the README.

### ⚡ Performance
- The Hermes plugin now runs `web_search_plus` and `web_extract_plus` in-process by default instead of spawning a `search.py` subprocess per call, removing interpreter-startup, module re-import, and JSON round-trip overhead on every tool invocation. The legacy subprocess path remains as an automatic fallback (used if the in-process import fails) and can be forced with `WSP_FORCE_SUBPROCESS=1`. A thread watchdog preserves the previous hard wall-clock timeout. (#50)
- Research mode now queries its providers concurrently instead of sequentially, so wall-clock cost tracks the slowest provider rather than the sum of all of them. Result ordering stays deterministic (preserved by submission order) and the time budget still gates which providers launch and whether extraction runs. (#50)

### 🐛 Fixed
- Standalone `search.py`, config helpers, and `setup.py status` now load provider keys from the active Hermes profile `.env` in addition to plugin-local legacy `.env` files, preventing false `missing_api_key` fallbacks when keys live in `~/.hermes/.env`. (#46)

### 📚 Docs
- Added README instructions for updating an installed plugin plus reload/reset notes. (#49, thanks @wysie)

### 🔧 Improved
- Provider retry backoff now adds bounded random jitter (`RETRY_JITTER_FRACTION`) so concurrent or repeated retries against a recovering provider no longer synchronize into bursts. (#50)
- Provider health read-modify-write is now guarded by a lock so concurrent in-process provider calls (parallel research mode) cannot lose cooldown updates. (#50)

### 🧱 Internal
- Split `search.py`'s monolithic `main()` into `build_parser()`, the pure `execute_search_request()` pipeline (returns `(payload, exit_code)` without printing or `sys.exit`), and in-process `run_search_request()`/`run_extract_request()` entry points. CLI behaviour and output are unchanged. (#50)

### 🧪 Tests
- Added in-process search coverage (provider config resolution, auto-routing, explicit-provider error dicts, empty-query guard), research-mode out-of-order completion ordering, retry jitter bounds, and subprocess-fallback behaviour. (#46, #50)

## [v2.3.1] — 2026-05-31

### 🐛 Fixed
- Fixed Hermes plugin loading by using package-relative provider registry imports with direct-import fallback compatibility.

## [v2.3.0] — 2026-05-29

### ✨ Added
- Added the ProviderSpec registry as the central source of truth for provider metadata across setup, config, routing, extraction, doctor diagnostics, and CLI choices.

### 🔧 Improved
- Quality reports now expose transparent authority signals for canonical-source routing classes, including canonical domain hits, demoted domain hits, and whether the top result is a primary source.

### 📚 Docs
- Documented the `search.py` compatibility-shim policy and removal path for the monolith-to-module split.

### 🧪 Tests
- Added offline golden snapshot quality checks for canonical source presence, blocked mirror domains, duplicate counts, and extracted-content substance without live provider calls.
- Added registry drift coverage so provider metadata stays synchronized across public surfaces.

## [v2.2.1] — 2026-05-25

### 🔧 Changed
- Split the large `search.py` implementation into focused cache, config, HTTP client, provider-health, provider, quality, research, routing, and extraction modules while keeping the public Hermes tool surface backward-compatible.
- Routed provider search/extraction calls through the new module boundaries without changing configured provider behavior.

### 🧪 Tests
- Added provider/extract contract tests plus HTTP client module coverage to lock the refactor down.
- Verified the full plugin test suite after syncing the refactor stack.

## [v2.2.0] — 2026-05-19

### ✨ Added
- Added Parallel as the 13th search provider and 6th extraction provider using `PARALLEL_API_KEY`. Parallel is available for explicit calls and remains guarded from auto-routing by default via `auto_allow=false`.

### 🔧 Changed
- `web_extract_plus(provider="auto")` now uses the benchmark-backed extraction fallback order Tavily → Exa → Linkup → Parallel → Firecrawl → You.com. Tavily becomes the fast reliable default head; Parallel provides a fast excerpt-rich docs fallback; Firecrawl remains the robust scraper safety net rather than the first call.
- Updated provider onboarding metadata and setup priority examples to include Parallel.

### 🧪 Tests
- Added regression coverage for Parallel search normalization, extraction normalization, explicit-only routing, onboarding metadata, and extraction fallback behavior.

## [v2.1.0] — 2026-05-16

### 🔥 Removed
- Removed `web_answer_plus` from the registered Hermes tool surface and plugin manifest. The plugin now keeps one job: search plus extraction, without a separate answer-synthesis layer.
- Removed runtime answer-mode metadata (`answer_mode_recommended`) and onboarding answer capability reporting.

### 📚 Docs
- Updated README, User Guide, FAQ, Architecture, and plugin manifest to describe the two-tool surface: `web_search_plus` and `web_extract_plus`.

## [v2.0.0] — 2026-05-15

### 🚀 Major: Routing v2
- Replaced naive provider-priority auto-routing with benchmarked, class-aware Routing v2 based on the 25-query provider matrix and qualitative provider review.
- You.com, Serper, Exa, Firecrawl, Tavily, and Linkup now form the conservative default search pool.
- Brave, SerpBase, Querit, Parallel, native Perplexity, and Kilo Perplexity default to explicit/guarded use via `auto_allow=false`; existing configs inherit these guarded defaults unless users explicitly opt providers back in.
- Added class-aware routing boosts for multilingual current queries, AT/local shopping, GitHub/docs, package/API docs, arXiv/academic queries, Reddit/community searches, CVE/security advisories, official/regulatory queries, finance/IR, weather/local factual lookups, OSS discovery, and answer/synthesis prompts.
- Search auto-routing now flags answer/synthesis prompts with `answer_mode_recommended` instead of selecting slow answer-only providers such as Kilo Perplexity.
- Routing diagnostics now expose `language_hint`, `routing_class`, and `routing_policy`.

### 📚 Docs
- Updated README, User Guide, FAQ, and Architecture docs for Routing v2 defaults, guarded providers, setup presets, and migration behavior.

### 🧪 Tests
- Added Routing v2 regression coverage for default auto-allow gates, legacy auto-allow migration, multilingual Japanese/Arabic routing to You.com, arXiv routing to Exa, Reddit/site queries away from Exa, Reddit-company finance queries, CVE/security routing away from Firecrawl, answer-mode recommendations, and sports-table false positives.

## [v1.10.0] — 2026-05-15

### ✨ Added
- Added SerpBase as a search provider using `SERPBASE_API_KEY`, available via `provider="serpbase"`.
- Added onboarding/config support for `auto_allow`, including `setup.py config set-auto-allow <provider> on|off` so experimental or fallback providers can remain explicit-only.

### 🔧 Changed
- SerpBase defaults to `auto_allow=false`: configured keys unlock explicit calls, but auto-routing/fallback will not select it unless users opt in. See [Architecture: Auto-allow gate](docs/ARCHITECTURE.md#auto-allow-gate).
- README provider, API-key, and routing docs now cover SerpBase activation and auto-allow behavior.
- Added detailed user documentation, FAQ, and architecture/trust-boundary docs under `docs/`.

### 🧪 Tests
- Added regression coverage for SerpBase response normalization, explicit provider calls, missing-key handling, onboarding metadata, and auto-routing exclusion.

## [v1.9.3] — 2026-05-14

### 🐛 Fixed
- `perplexity` now uses the native Perplexity API endpoint (`https://api.perplexity.ai/chat/completions`), `PERPLEXITY_API_KEY`, and `sonar-pro` model instead of the Kilo gateway defaults.
- `kilo-perplexity` is preserved as a distinct routing provider using `KILOCODE_API_KEY`, the Kilo gateway endpoint, and `perplexity/sonar-pro`.

### 🧪 Tests
- Added regression coverage for native Perplexity defaults, distinct Kilo Perplexity routing, separate environment keys, and onboarding/runtime config normalization.

## [v1.9.2] — 2026-05-10

### 🔧 Changed
- `web_extract_plus(provider="auto")` now documents and tests the intended extraction fallback order: Firecrawl → Linkup → Exa → Tavily → You.com. Firecrawl remains the robust default scraper, Linkup stays the cheap/citation-friendly fallback, and Exa is tried before Tavily for research-style pages.

### 🧪 Tests
- Added regression coverage for the direct extraction provider priority and Exa-before-Tavily fallback behavior.

## [v1.9.1] — 2026-05-09

### 🐛 Fixed
- Accept both `kilo-perplexity` and `kilo_perplexity` as routing aliases for the Perplexity/Kilo bridge in setup and runtime config loading.
- Prevent same-second config quarantine/backup filename collisions from overwriting earlier broken-config artifacts.

### 🧪 Tests
- Added regression coverage for underscore Kilo/Perplexity aliases and repeated same-second runtime config quarantines.
- Verified the full onboarding/config surface with isolated CLI smoke tests and runtime semantic checks.

### 🙏 Contributors
- @robbyczgw-cla

## [v1.9.0] — 2026-05-09

### ✨ Added
- Added provider-behavior onboarding commands under `setup.py config` so users can choose fixed-provider mode, re-enable auto-routing, set routing priority, set fallback provider, disable/enable providers, tune confidence threshold, and reset config with backup.
- Added JSON status output that reports configured provider capabilities plus routing preferences without printing secrets.
- Added `--config-path` and `WEB_SEARCH_PLUS_CONFIG` support for isolated tests and non-default behavior config locations.
- Added setup-time routing flags (`--routing`, `--default-provider`, `--provider-priority`, `--disable-providers`, `--fallback-provider`, `--confidence-threshold`) so first-run onboarding can configure keys and preferences together.

### 🔧 Improved
- `--provider auto` now respects persisted fixed-provider mode when auto-routing is disabled.
- Corrupt behavior config files are moved aside safely and replaced with defaults instead of crashing onboarding.
- Routing config writes are atomic; reset creates timestamped backups.

### 🧪 Tests
- Expanded onboarding coverage to 40 tests, including config commands, dry-runs, corrupt config recovery in both setup and runtime paths, no-secret leak checks, fixed-provider routing behavior, and Kilo/Perplexity alias normalization.

## [v1.8.1] — 2026-05-09

### 🔧 Changed
- Reframed `web_answer_plus` as an **optional beta answer-synthesis layer**, not a default replacement for `web_search_plus`.
- Tightened the registered tool description so agents prefer `web_search_plus` for current events, sports lineups, schedules, scores, standings, prices, weather, and raw source discovery.
- Changed the default `web_answer_plus` freshness behavior from `auto` to `none`. Set `freshness="auto"`, `day`, `week`, `month`, or `year` explicitly when recency should shape source selection.

### 📚 Docs
- Added clear “use `web_search_plus` first” guidance for fast/current/source-discovery queries.
- Added `web_answer_plus` beta guidance, pros/cons, and a dogfooded failure case around Austrian football lineup/current-query drift.
- Updated README examples to show answer synthesis as opt-in and freshness as explicit.

### 🧪 Tests
- Added regression coverage proving default `web_answer_plus` calls do **not** apply a freshness filter.
- Test suite: 82/82 unit tests passing locally.

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
