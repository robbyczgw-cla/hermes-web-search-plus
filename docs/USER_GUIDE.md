# web-search-plus User Guide

This guide is the long-form operating manual for `web-search-plus`. If you only need the first install, start with the [README Quick Start](../README.md#quick-start). Come back here when you want to tune providers, routing, fallback, or extraction without guessing.

## What this plugin does

`web-search-plus` adds two Hermes tools:

- `web_search_plus` for routed multi-provider web search.
- `web_extract_plus` for clean URL extraction.

The plugin is capability-based. You do not need every provider key. One search-capable key is enough for search; one extraction-capable key unlocks URL extraction.

## Installation and first-run checks

Install and enable the plugin:

```bash
hermes plugins install robbyczgw-cla/hermes-web-search-plus --enable
```

Check status and configure keys:

```bash
hermes web-search-plus status
hermes web-search-plus setup
hermes config set BRAVE_API_KEY <key>
```

Hermes Desktop has the same key fields under Settings > Plugins > Web Search Plus and keeps keys out of your shell history.

After the first install, restart Hermes once so the tools are registered (CLI: exit and start `hermes` again; gateway: `/restart`). A new or changed key applies from your next message on the gateway (Telegram, Discord, API); in the interactive CLI run `/reload` first. To switch a provider off, set its key to an empty value (`hermes config set BRAVE_API_KEY ""`): `hermes config unset` removes the line, but a key Hermes does not know by name stays active until Hermes restarts.

**Upgrading from 5.0 or earlier?** Until 5.0 you could keep keys in a `.env` file in the plugin folder or in `~/.hermes/plugins/.env`. Since 5.1 those files are not read under Hermes (and Web Search Plus never opens them, not even to check what is inside). If `status` shows `! A .env file in ... is no longer read since 5.1`, open that file and store each key in your Hermes profile under the same name, for example `hermes config set BRAVE_API_KEY <key>`, then run `/reload`. Until you do, `web_search_plus` and `web_extract_plus` stay visible but answer with these steps instead of searching. `status --json` lists the files in `legacy_env_files`. The MCP server and the standalone CLI still read `.env` files as before.

**Make Hermes' own `web_search` use your keys.** Hermes shows its built-in `web_search`/`web_extract` to the model by default and may hide plugin tools behind Tool Search, so the model can answer without ever calling Web Search Plus. With at least one search key configured, `status` and `setup` therefore print how to route Hermes' own tools through Web Search Plus (the native backend, [details](NATIVE_BACKEND.md)):

```bash
hermes config set web.search_backend wsp
hermes config set web.extract_backend wsp   # with an extraction key (Tavily, Exa, Linkup, Firecrawl, ...)
hermes config set web.extract_backend exa   # search-only keys such as Brave: keep Hermes' own extraction
```

Run `/reload` afterwards. Always set `web.extract_backend` together with `web.search_backend wsp`: if it is not set, Hermes picks `wsp` for extraction as well. Choose `wsp` only if one of your keys can extract pages (Brave, for example, only searches); otherwise every `web_extract` fails. Keep a Hermes backend such as `exa` (keyless tier) or `tavily` instead. `status` warns about both mistakes. The alternative is to keep Hermes' search and list the Plus tools under `tools.tool_search.never_defer` in your Hermes `config.yaml`: `[web_search_plus, web_extract_plus]`. Web Search Plus never writes your Hermes config; `status` only reads the `web` settings (`status --json`: `native_backend_hint`).

Smoke test from the plugin directory. The standalone script does not read `.env` files, so export the keys first:

```bash
cd ~/.hermes/plugins/web-search-plus
set -a; . ~/.hermes/.env; set +a
python3 search.py --query "Hermes Agent latest release" --provider auto --quality-report --compact
```

Inspect whether the install is likely to hit the low-latency Hermes fast path:

```bash
hermes web-search-plus fastpath
hermes web-search-plus fastpath --json
```

The fast-path checker is intentionally advisory and stdlib-only. It verifies that the plugin declares both tools for direct registration, that the standalone setup helper is available, and whether your Hermes `config.yaml` contains the current public-Hermes hint below. It does not require Hermes core patches.

```yaml
agent:
  disabled_toolsets: [web]
```

Use this when Web Search Plus should be the preferred web layer. Without it, Web Search Plus still works; Hermes may simply have more web-capable tools to choose from. Some forks/local builds may expose additional tool-pinning config, but this guide only documents options available in current public Hermes.

### Bench your providers

Once keys are configured, benchmark the providers against each other and get a data-backed `auto_routing.provider_priority` suggestion:

```bash
hermes web-search-plus bench
# or, from the plugin directory with the keys exported:
python3 search.py --bench
python3 search.py --bench --json   # structured report
```

The bench runs a small fixed query suite (docs, vendor release, community, non-English) against every configured search-capable provider and reports success rate, median latency, result volume, and simple quality signals (duplicate-free URLs, snippet coverage). Providers are ranked by a weighted score — reliability first, then speed, then quality — and the recommended priority is printed together with the exact `config set-priority` command to apply it. With the default `auto_routing.order: measured`, `provider_priority` only orders the fallback chain; to make the bench order the first choice for every query, apply the same list with `config set-order` instead.

Two guarantees worth knowing:

- Bench calls providers directly, so a bench run never triggers provider cooldowns and never feeds the latency statistics that time the fallback.
- Nothing is written to your config; applying the recommendation is always an explicit step.

Note that the bench makes a few real API calls per provider, so it spends a small amount of quota on every configured provider.

## Provider setup

Keys live in the active Hermes profile. Store them with `hermes config set <NAME> <key>` or in Hermes Desktop; Web Search Plus reads them through Hermes and never prints secret values. See the generated [provider reference](PROVIDERS.md) for every provider's capabilities, env var, auto-routing default, free tier, and signup link.

Useful commands:

```bash
hermes web-search-plus list
hermes web-search-plus status --json
hermes web-search-plus setup --preset starter
hermes web-search-plus setup you linkup
```

Presets:

- `starter`: Brave + Serper + Exa + Linkup. The providers automatic routing uses first; this is what `hermes web-search-plus setup` starts with by default.
- `lean`: Brave + Linkup. Small fast search plus extraction.
- `search`: Brave + Serper + Exa + Tavily + Firecrawl + Linkup. The full default search pool.
- `extract`: Linkup + Firecrawl + Tavily. Extraction-heavy setup.
- `self-hosted`: SearXNG + keyless Keenable for automatic routing without a commercial API key. A separately installed DonSeTch sidecar can be layered on for explicit local search and extraction.
- `all`: every supported provider.

Search-capable providers include You.com, Serper, Exa, Firecrawl, Tavily, Linkup, Parallel, Brave, SearXNG, SerpBase, Querit, Keenable, and the optional local DonSeTch MCP sidecar. Extraction-capable providers are Linkup, Firecrawl, Tavily, Exa, Parallel, You.com, Keenable, Serper, and DonSeTch.

Keenable is keyless: set `KEENABLE_API_KEY` for the authenticated endpoints, or opt into its public tier (off by default). Without any search key, setup asks whether to start on it (default no); or run `hermes web-search-plus setup keenable --keyless-public`; it writes `keenable.allow_public: true` to `config.json` (equivalently `KEENABLE_ALLOW_PUBLIC=1`).

Under Hermes, `config.json` is shared between profiles, so `<provider>.allow_public` (e.g. `keenable.allow_public`) is ignored there. Set it in the profile instead with `hermes config set KEENABLE_ALLOW_PUBLIC 1`, then run `/reload`. `hermes web-search-plus status` lists any such ignored flag with the fix. The setup flag above is not affected when WSP runs outside Hermes (MCP, standalone CLI).

DonSeTch is a different kind of keyless provider: it is a separately installed local MCP service rather than a public WSP endpoint. Set `hermes config set DONSETCH_BIN /absolute/path/to/donsetch` (or the DonSeTch binary field in Hermes Desktop), then call `provider="donsetch"` explicitly. DonSeTch stays outside automatic routing and fallback unless `hermes web-search-plus config set-auto-allow donsetch on` is set. See [DonSeTch local MCP provider](DONSETCH.md) before enabling it; keyless removes commercial credentials and request billing, not local resource use, outbound traffic, rate limits, or website policy obligations.

With a `KEENABLE_API_KEY` set, requests always use the authenticated endpoints. Without a key and without the opt-in, Keenable is treated as unconfigured: it won't auto-route, fall back, or enable `web_extract_plus`. When the public tier is enabled, queries and fetched URLs are sent to an **unauthenticated** public service with per-IP limits and no SLA — roughly 1,000 requests/hour and 10 requests/second — so treat it as a best-effort last resort, not a dependable provider. The first request that uses the public endpoint logs a one-time warning so the egress is visible, and `web-search-plus doctor` reports keyless providers as `key=no` with a separate `keyless=on/off` badge so key status stays truthful.

### Local and keyless paths

`Local`, `self-hosted`, and `keyless` describe different boundaries in WSP:

- **SearXNG** is an operator-configured metasearch endpoint and supports search only. The `self_hosted` profile can select it automatically.
- **DonSeTch** is a separately installed local stdio MCP provider and supports both search and extraction. It is explicit-only by default and is not installed or silently enabled by the `self-hosted` preset.
- **Keenable's public tier** needs no API key, but it is a remote public service rather than a local provider.

Neither SearXNG nor DonSeTch makes web access offline: searches still reach upstream engines, and extracted URLs still reach destination websites. "Local" means that you operate the control-plane service and its compute, not that requests stay on the machine.

### Self-hosted automatic-routing profile

Use the `self_hosted` profile when automatic traffic must avoid commercial API keys. The quickest setup is:

```bash
hermes web-search-plus setup --preset self-hosted
hermes web-search-plus status
```

The preset records the profile and enables Keenable's existing keyless public tier. Set the optional SearXNG endpoint with `hermes config set SEARXNG_INSTANCE_URL <url>` (then `/reload`). Under Hermes this is the only place it is read: a `searxng.base_url` or `searxng.instance_url` in `config.json` is ignored, because the URL can carry credentials and `config.json` is shared between profiles; `status` tells you when one is set. Outside Hermes (MCP, standalone CLI) you can configure it directly in `config.json`; use the canonical `searxng.base_url` name (legacy `searxng.instance_url` remains supported):

```json
{
  "profile": "self_hosted",
  "searxng": {
    "base_url": "https://search.example.net"
  }
}
```

At load time, rather than by saving duplicate routing settings, the profile derives this effective automatic policy:

- Search auto pool: SearXNG and Keenable only.
- Fallback: keyless-capable Keenable.
- Extraction auto pool: keyless-capable Keenable only.
- Budget preflight limits continue to apply unchanged when configured.

`hermes web-search-plus status` is an offline diagnostic: it displays the active profile and effective auto pool, then checks that the SearXNG URL is present and well formed and that Keenable is configured or its keyless public tier is enabled. It never contacts either provider. If neither SearXNG nor Keenable is usable, an automatic request returns the typed `self_hosted_profile_unavailable` error with the local remediation.

#### Add DonSeTch for local Search + Extract

The `self-hosted` preset deliberately does not install DonSeTch or add it to the automatic pool. Install DonSeTch separately as described in [DonSeTch local MCP provider](DONSETCH.md), then set its executable path for the Hermes process:

```bash
export DONSETCH_BIN=/absolute/path/to/donsetch
```

Use DonSeTch explicitly for both capabilities:

```python
web_search_plus(query="Hermes Agent documentation", provider="donsetch", count=5)
web_extract_plus(urls=["https://example.com"], provider="donsetch")
```

This gives a local-first control plane with SearXNG available for automatic search and DonSeTch available for deliberate search or extraction. DonSeTch remains outside automatic routing and fallback unless an operator explicitly opts it in after measuring local reliability and latency.

The profile governs only automatic routing. An explicit `provider="serper"` (or another keyed provider) still works when its key exists; its result metadata includes `"profile_deviation": true` so the paid-key deviation is visible. Explicit provider requests remain useful for diagnostics and controlled operator overrides.

### Migration note for v2.0.0

Routing v2 changes the default `provider="auto"` behavior. Existing configs keep explicit user choices, but missing `auto_allow` entries inherit the guarded defaults: SerpBase and Querit stay explicit-only until you opt them into automatic routing; Parallel joins the default auto-pool at priority 8 when a key is configured. (Since 5.0, Brave is first in the default priority; see [Routing](ROUTING.md).) Removed Perplexity and Kilo-Perplexity IDs in older configs are ignored; other unknown provider IDs still invalidate the config.

```bash
python ~/.hermes/plugins/web-search-plus/setup.py config show --json
python ~/.hermes/plugins/web-search-plus/setup.py config set-auto-allow serpbase on
python ~/.hermes/plugins/web-search-plus/setup.py config set-auto-allow serpbase off
```

## Routing preferences

For a generated reference of each intent, its first provider and the fallback chain, see [Routing Reference](ROUTING.md).

Secrets and behavior are intentionally separate:

- Provider keys live in the Hermes profile (`hermes config set`, Hermes Desktop).
- Routing behavior lives in `config.json`.
- API keys do not belong in `config.json` under Hermes: store them with `hermes config set <PROVIDER>_API_KEY <key>`. Under Hermes a `api_key`/`apiKey` found in `config.json` is ignored (`hermes web-search-plus status` says so, without printing the value); the MCP server, OpenClaw skill and standalone CLI still read it. Status, `config show --json` and dry-run output never print key values.
- DonSeTch also ignores `donsetch.api_key` as an executable path under Hermes. The explicit `donsetch.binary` field remains allowed because it is a path, not a secret; Hermes Desktop stores `donsetch_bin` in the profile's `config.yaml` settings and overlays that field. `DONSETCH_BIN` and forwarded `DONSETCH_*` values come through the active profile resolver. Additional variable names must be present in the process environment to be discovered (the resolver cannot list names); their process values are not used under Hermes. Standalone hosts keep the legacy `api_key` path fallback and environment behavior.
- `WEB_SEARCH_PLUS_CONFIG=/path/to/config.json` can point runtime search at a custom config.
- `setup.py config <subcommand> --config-path /path/to/config.json` points a config subcommand at a custom config.

Inspect routing:

```bash
python ~/.hermes/plugins/web-search-plus/setup.py config show --json
```

Pin a fixed provider:

```bash
python ~/.hermes/plugins/web-search-plus/setup.py config set-default you
```

Turn query-based auto-routing back on:

```bash
python ~/.hermes/plugins/web-search-plus/setup.py config set-routing on
```

Tune automatic search routing, extraction routing, and fallback:

```bash
python ~/.hermes/plugins/web-search-plus/setup.py config set-priority you,serper,exa,firecrawl,tavily,linkup
python ~/.hermes/plugins/web-search-plus/setup.py config set-extract-priority serper,parallel,tavily,exa,linkup,firecrawl,you,keenable
python ~/.hermes/plugins/web-search-plus/setup.py config set-fallback serper
python ~/.hermes/plugins/web-search-plus/setup.py config disable brave
python ~/.hermes/plugins/web-search-plus/setup.py config enable brave
python ~/.hermes/plugins/web-search-plus/setup.py config set-threshold 0.45
```

Preview config changes without writing:

```bash
python ~/.hermes/plugins/web-search-plus/setup.py config set-default you --dry-run
```

Semantics worth knowing:

- `set-default <provider>` disables auto-routing and makes `--provider auto` resolve to that provider; `set-routing on` restores query-based routing while keeping the saved default for later.
- `set-priority` changes search auto-routing and fallback priority only.
- `set-extract-priority` changes `provider="auto"` extraction order only. It accepts extract-capable providers, removes duplicates, and appends omitted extract providers in registry order.
- `set-auto-allow <provider> off` keeps a configured provider available for explicit calls while preventing auto-routing/fallback from selecting it.
- `config reset --yes` backs up the existing file before writing fresh defaults.

## V3 budget preflight

Budget preflight is opt-in. Before a native v3 request starts any provider attempt, it can cap the provider-call fan-out, request wall-time budget, extraction context, and the current UTC day's provider-call ledger. The default is disabled and all limits are unbounded, so existing requests are unchanged.

```json
{
  "budget_preflight": {
    "enabled": true,
    "max_provider_calls_per_request": 2,
    "max_daily_provider_calls": 100,
    "max_timeout_seconds": 30,
    "max_context_chars": 30000,
    "on_exceed": "degrade"
  }
}
```

Set a limit to `null` to leave that dimension unbounded. With `on_exceed: "degrade"`, WSP deterministically applies the smallest compatible cap; with `"abort"`, it returns a typed budget failure before a provider call. Daily usage is checked and recorded in the existing local v3 `budget_ledger`; an unavailable ledger fails closed when a daily quota is enabled. The per-request attempt cap (`max_provider_attempts`) lives in memory and stays enforced even when the state database is unavailable. The receipt endpoint includes the typed checks and any reduction or abort reason. Set `WSP_BUDGET_PREFLIGHT_OFF=1` for an emergency process-level override; `0`, `false`, `no`, and `off` leave configured preflight enabled.

One unit of the request and daily budgets is one provider try that the attempt engine admits; a retry is a second unit. Most tries are one HTTP request. A batch extraction try (Firecrawl, Linkup, Serper, Keenable, DonSeTch) fetches each URL of the batch with its own request but counts as one unit. No provider request starts after the request deadline: a batch keeps the pages it already fetched and marks the rest `not tried: request ran out of time`.

### GroktoCrawl / local Firecrawl-compatible backends

Firecrawl search and extraction use configurable endpoint URLs. If you run a local Firecrawl-v2-compatible service such as [GroktoCrawl](https://github.com/groktopus/groktocrawl), point the existing `firecrawl` provider at that service instead of adding a new provider name:

```json
{
  "firecrawl": {
    "api_url": "http://127.0.0.1:8080/v2/search",
    "scrape_url": "http://127.0.0.1:8080/v2/scrape"
  }
}
```

The backend still receives the same bearer header WSP sends for Firecrawl, so set `FIRECRAWL_API_KEY` when the local service requires authentication. This is an operator-controlled override: WSP keeps the default Firecrawl cloud URLs unless you set these config values. The GroktoCrawl path has been smoke-tested for search and scrape/extract response compatibility, but monitor your own timeout, pagination, and rate-limit behavior before relying on it for production crawls.

This local endpoint override is separate from the safety check on extraction **target** URLs. `web_extract_plus` rejects private/internal targets by default, including CGNAT/shared-address ranges, IPv6 ULA/link-local/mapped-private addresses, multicast, cloud metadata, and DNS answers that point inward. It still allows the operator-configured local `firecrawl.scrape_url` above. If you intentionally want to extract trusted intranet pages, opt in explicitly:

```json
{
  "extract": {
    "allow_private_urls": true
  }
}
```

The guard validates the initial extraction target before any provider or fallback runs. It rejects URLs that Python and a browser-grade (WHATWG) parser could read differently: backslashes, whitespace and control characters, userinfo, percent-escaped hostnames, and ambiguous numeric IPv4 forms such as `0177.0.0.1`, `0x7f.1` or `2130706433`. Internationalized hostnames such as `müller.de` are converted to punycode (IDNA 2008 when the optional `idna` package is installed, otherwise the standard library codec) and the converted URL is what gets validated and fetched; labels that mix scripts or use compatibility characters are rejected. It then checks literal IPs (including IPv4-mapped and NAT64 IPv6) and every DNS answer for loopback, private, link-local, multicast and other non-global ranges.

**Residual risk: this is a pre-flight check, not a network boundary.** The URL is handed to a remote extractor or browser that resolves DNS and follows redirects on its own. A host can answer with a public address during the check and an internal one during the fetch (DNS rebinding), and a public page can redirect to an internal address. Neither can be closed from inside WSP. If the final fetcher can reach internal networks, enforce egress on the fetcher itself: block private, loopback, link-local and metadata ranges at its network layer, validate every redirect hop and every resolved address at connect time, and run it without credentials or access to internal services. A self-hosted backend such as GroktoCrawl needs exactly this policy.

### Untrusted web content

Search and extract results contain titles, snippets, URLs and page text written by third parties. Tool output now starts with a notice saying so before any of that text appears. It is defense in depth for the calling agent, not a sandbox: do not pass result text into privileged tool parameters, and do not follow instructions found in it. Provider error text is never reflected to the model either; errors carry a fixed message plus the HTTP or provider status code.

### Routing debug walkthrough

When a query does not use the provider you expected, ask for routing diagnostics instead of guessing:

```bash
python3 search.py --query 'best wireless earbuds under $100' --provider auto --quality-report --compact --no-cache
```

In the JSON output, check these fields first:

- `routing.provider`: the selected provider.
- `routing.reason`: `intent_<name>` when the query matched an intent (`academic`, `community`, `docs`, `local`, `news`, `security`, `shopping`), `no_signals_matched` when it is a general query.
- `routing.analysis_summary.routing_class`: the intent the query was classified as; `routing.analysis_summary.intent_signals` lists the cues that decided it.
- `quality_report.skipped_providers`: providers skipped because of cooldown or errors.
- `routing.auto_allow_excluded`: configured providers that were blocked from automatic routing by `auto_allow=false`.
- `quality_report.extraction_recommended`: whether snippets look thin enough that `web_extract_plus` may help.

Example pattern:

```json
{
  "routing": {
    "provider": "serper",
    "reason": "intent_shopping",
    "routing_policy": "routing-v3",
    "routing_class": "shopping",
    "auto_allow_excluded": ["serpbase"]
  },
  "quality_report": {
    "skipped_providers": [
      {"provider": "brave", "reason": "cooldown", "cooldown_remaining_seconds": 42}
    ]
  }
}
```

Read that as: guarded providers can have keys but remain explicit-only for `provider="auto"`, and the router selected the first eligible provider for the query's intent. If you want SerpBase or Querit to participate in automatic routing, opt in with `set-auto-allow <provider> on`; if a provider is cooled down, wait or inspect local provider health state.

## Search locale defaults

Providers with region/language request parameters (Serper, Brave, You.com, SerpBase, Querit, Firecrawl, SearXNG) no longer hardcode us/en. Set your defaults once in `config.json`:

```json
{
  "defaults": {
    "locale": {
      "country": "at",
      "language": "auto"
    }
  }
}
```

- `country`: ISO 3166-1 alpha-2 code (for example `at`, `fr`, `es`) used as the default region for locale-aware providers.
- `language`: ISO 639-1 code (for example `de`), or `"auto"` to infer the language from each query. The `--language` flag and the `language` tool parameter accept `auto` for a single call.

`"auto"` mode uses a lightweight, local stopword/character heuristic (no LLM, no extra dependency, no IP geolocation) covering `de`, `es`, `fr`, `it`, `pt`, `nl`, and `en`, plus Japanese (kana), Russian (Cyrillic), and Arabic by script. It is deliberately conservative: it needs at least two distinct language signals and a single unambiguous winner, or a script that identifies the language. Conflicting evidence (for example kana inside an English sentence) and Han-only text (Chinese or Japanese) infer nothing. A query like "Wiener Kaffeehaus Öffnungszeiten" infers German; a terse technical query like "DAC R2R NOS" or "PostgreSQL 17 release notes" infers nothing, and then **no language parameter is sent**, so the provider's own default applies. Without `"auto"` the language still defaults to `en`.

Explicit location hints in the query move the country: a small curated table of well-known city and country names (Vienna/Wien, Berlin, Paris, Madrid, London, Rome, Amsterdam, ...) is checked, so "mejores restaurantes Madrid" searches with `country=es` and "boulangerie Paris horaires" with `country=fr` even when your configured default is `at`. Conflicting hints ("compare bakeries in Paris and Madrid") change nothing.

Resolution precedence:

1. CLI flags / tool parameters (`--country` / `--language`, or the `country` / `language` tool parameters); a language of `auto` replaces `defaults.locale.language` for that call
2. Explicit provider-specific config in `config.json` (for example `serper.country` or `brave.search_lang`); it also wins over `auto`
3. Explicit location hint in the query (country only)
4. `defaults.locale.country` / `defaults.locale.language` (with `"auto"` triggering language detection)
5. Fallback `us` / `en` (language: nothing is sent when `auto` detects nothing)

**Query language does not imply country.** A German query can come from Austria or Switzerland just as well as Germany, so inferred language never moves the region — only explicit location hints, configuration, or flags do.

Result metadata reports the resolved locale and where each value came from, following the freshness-metadata pattern:

```json
"locale": {
  "country": "at",
  "language": "de",
  "source": {"country": "config", "language": "inferred"}
}
```

`source.language` is `config`, `cli`, `inferred`, `jev`, `fallback`, or `provider_default` (`auto` found nothing, `language` is `null`, and the request carries no language parameter).

Backward compatibility: without `defaults.locale` and without flags, everything still resolves to `us`/`en` exactly as before. Providers without locale parameters (Tavily, Exa, Linkup, Parallel, Keenable) are unaffected.

## Explicit opt-in providers: guarded providers

Some providers can be configured for explicit use without being selected automatically. That is what `auto_allow` controls.

SerpBase and Querit default to `auto_allow=false`. Setting their keys makes explicit calls work; configured Brave and Parallel keys participate in automatic routing by default:

```python
web_search_plus(query="best DAC reviews", provider="serpbase")
web_search_plus(query="aktuelle KI-News Deutschland", provider="querit")
```

That does not make any guarded provider eligible for automatic routing or fallback until you opt in:

```bash
python ~/.hermes/plugins/web-search-plus/setup.py config set-auto-allow serpbase on
python ~/.hermes/plugins/web-search-plus/setup.py config set-auto-allow querit on
```

Turn automatic use back off:

```bash
python ~/.hermes/plugins/web-search-plus/setup.py config set-auto-allow serpbase off
python ~/.hermes/plugins/web-search-plus/setup.py config set-auto-allow querit off
```

This pattern avoids silent cost or coverage surprises. Use it for providers whose pricing, maturity, or result style you want to test before letting `provider="auto"` choose them.

## Using `web_search_plus`

Use `web_search_plus` when you need source discovery, current facts, prices, schedules, weather, sports, or provider diagnostics.

Examples:

```python
web_search_plus(query="Graz weather today")
web_search_plus(query="best bookshelf speakers under 1000 EUR", quality_report=True)
web_search_plus(query="alternatives to Notion", provider="exa")
web_search_plus(query="turntable reviews under 1000", mode="research", research_time_budget=45)
```

Parameters:

| Parameter | Type | Default | Description |
|---|---|---|---|
| `query` | string | **required** | Search query |
| `provider` | string | `"auto"` | `auto`, `serper`, `brave`, `tavily`, `exa`, `linkup`, `firecrawl`, `parallel`, `you`, `searxng`, `serpbase`, `querit`, `keenable` |
| `depth` | string | `"normal"` | Exa only: `normal`, `deep`, `deep-reasoning` |
| `count` | integer | `5` | Results, 1–20 |
| `time_range` | string | — | `day`, `week`, `month`, `year` |
| `freshness` | string | — | Unified recency filter: `day`, `week`, `month`, `year` (case-insensitive) |
| `search_type` | string | `"search"` | Result vertical: `search` or `news` |
| `include_domains` | string[] | — | Restrict search to domains |
| `exclude_domains` | string[] | — | Exclude domains |
| `quality_report` | boolean | `false` | Include routing diagnostics, provider scores, result counts, authority signals, and extraction recommendation |
| `mode` | string | `"normal"` | `normal` or opt-in `research` |
| `research_time_budget` | number | `55.0` | Best-effort seconds budget for research mode |

Parameter semantics:

- `provider`: `auto`, or a concrete provider such as `you`, `serper`, `exa`, `firecrawl`, `tavily`, `linkup`, `brave`, `parallel`, `searxng`, `serpbase`, `querit`, or `donsetch`. Brave is first in the default auto-pool; Parallel joins at priority 8 when a key is configured. SerpBase, Querit, and DonSeTch remain available for explicit calls but default to `auto_allow=false`.
- `count`: result count, from 1 to 20.
- `time_range`: `day`, `week`, `month`, or `year` where supported.
- `freshness`: unified recency filter with the values `day`, `week`, `month`, or `year` (case-insensitive; invalid values return a clear error). It is applied natively by Serper, Brave, Querit, Firecrawl, Keenable, You.com, SearXNG, Exa, Tavily, and TinyFish, each translated into that provider's own format (for example Brave `pw`, Serper `tbs=qdr:w`, Exa absolute UTC `startPublishedDate`/`endPublishedDate` bounds, Tavily `time_range`, or TinyFish publication-time filters). Providers without recency support (Linkup, Parallel, SerpBase) still run the search normally; result metadata reports `freshness.applied=false` instead of silently dropping the filter. In `mode="research"` the applied status is reported per provider.
- `search_type`: result vertical, `search` (default) or `news` (case-insensitive; invalid values return a clear error). Serper serves news natively via `google.serper.dev/news`; TinyFish serves it through its native news-domain mode. Providers without a native news vertical still run the normal search; result metadata reports `search_type.applied=false` instead of silently ignoring the request. In `mode="research"` the applied status is reported per provider.
- `include_domains` / `exclude_domains`: domain filters, as a list or one string separated by commas or spaces. Providers with a domain field get them natively; Brave, Serper, SerpBase, You.com and Firecrawl get `site:` / `-site:` operators built from bare hostnames (`.gov` filters by suffix). An `include_domains` value without a usable domain is an error rather than an unrestricted search. Tavily cannot filter by a suffix such as `.gov` (it takes `*.example.com`); automatic search skips it for suffix filters.
- `quality_report`: include routing diagnostics, skipped providers, result quality hints, and extraction recommendation.
- `mode="research"`: query multiple providers and optionally extract selected URLs within a best-effort wall-clock budget.

Google source redirects: Serper and other Google-backed providers can return opaque `google.com/goto?url=…` links. WSP resolves them before ranking and caching, with at most five concurrent GETs, two seconds per link and the request's remaining deadline. Only one redirect hop to a public HTTP(S) destination is accepted; unusable links are dropped and resolved duplicates are removed. Classic `/url?q=` and `/url?url=` links are decoded locally.

Privacy: resolution sends one GET per opaque wrapped link from your machine to Google (normally `google.com`); Google sees your IP address and the redirect token. WSP does not fetch the destination page. Set `"serper": {"resolve_redirects": false}` in `config.json` to drop wrapped sources without these requests, which can yield fewer results. This setting also applies to wrappers from other providers. Cache hits reuse the cleaned URLs without resolving again; changing this setting starts a separate cache entry.

JSON result `metadata` includes `redirects_wrapped`, `redirects_resolved` and `redirects_dropped` when wrappers were present. Resolved includes locally decoded links and links later removed as duplicates; dropped counts only unresolvable/disabled wrappers. Native v3 JSON exposes the same counts in the `wsp.search.redirects` warning's `details`.

Reading the text output: after a security notice line, the header line names the serving provider, then how it was routed, then machine-readable `key=value` bits. Clients that parse the text should ignore bits they do not know.

```text
[Provider: exa | auto-routed | low confidence | intent_general | results=1 | elapsed_ms=37 | fallback_from=serper,brave]
[Provider: serper | cached 7s ago | auto-routed | low confidence | intent_general | results=1 | elapsed_ms=3]
[Provider: research | auto-routed | low confidence | intent_general | providers=brave,exa,keenable | pages=3 | results=5 | elapsed_ms=12400]
```

- `results`: results in this output, also `0` for a search that found nothing.
- `redirects_dropped`: appended at the end only when Google wrapper sources were dropped (`N > 0`).
- `elapsed_ms`: wall time of this call; for a cache hit, the time of the lookup.
- `fallback_from`: providers that failed or came back empty before the serving one, in order. Absent without a fallback.
- `providers` and `pages` (research only): the providers whose results were merged and the number of pages read.
- A cache hit repeats how the cached search was routed. Entries cached before 5.1.2 show no routing.

Each result is `N. title` (with `[published YYYY-MM-DD]` when known), then the URL and the snippet indented by three spaces, then a blank line. Every snippet line is indented, so page text can never start a line of its own.

## Using `web_extract_plus`

Use `web_extract_plus` when you already have URLs and want page content, not just search snippets.

```python
web_extract_plus(urls=["https://example.com"], provider="firecrawl")
web_extract_plus(urls=["https://docs.linkup.so"], provider="linkup", render_js=False)
```

Parameters:

| Parameter | Type | Default | Description |
|---|---|---|---|
| `urls` | string[] | **required** | URLs to extract |
| `provider` | string | `"auto"` | `auto`, `tavily`, `exa`, `linkup`, `parallel`, `firecrawl`, `you`, `keenable`, `serper` |
| `format` | string | `"markdown"` | `markdown` or `html` |
| `include_images` | boolean | `false` | Include image metadata when supported |
| `include_raw_html` | boolean | `false` | Include raw HTML when supported |
| `render_js` | boolean | `false` | Render JavaScript before extraction when supported |

Auto extraction defaults to Tavily, Exa, Linkup, Parallel, Firecrawl, You.com, Keenable, then Serper when those providers are configured. DonSeTch is extraction-capable but excluded from automatic extraction and fallback unless explicitly opted in. Change only this order with `setup.py config set-extract-priority ...`; the setting is stored as `auto_routing.extract_provider_priority` and does not inherit search `provider_priority`. Partial lists are completed with missing extract providers in registry order. Serper's webpage scraper (`https://scrape.serper.dev`, overridable via config `serper.scrape_url`, timeout via `serper.extract_timeout`) remains the public default's last-resort fallback. Each URL is returned independently; one failed URL does not discard successful results from the same call.

Parallel extraction explicitly requests `full_content`. Its default budget is 60,000 characters per result and 120,000 characters total so long documents are not unfairly shortened compared with other extraction providers. Operators can override those request-side limits in `config.json` with `parallel.max_chars_per_result` and `parallel.max_chars_total`. Search quality/latency can be tuned with `parallel.mode` (`turbo`, `fast`, `basic`, `advanced`); the default is `fast`. Use `advanced` or research mode for deeper work. Parallel now joins automatic routing when configured.

Large extracted pages are not returned as raw token bombs. `web_extract_plus` sanitizes inline base64 images, stores the full cleaned text under `cache/web`, and returns a bounded head/tail preview with a footer containing the stored file path plus an exact `read_file(path=..., offset=..., limit=500)` call for paging into the omitted middle. Configure the inline budget in `config.json`:

```json
{
  "web": {
    "extract_char_limit": 15000
  }
}
```

If the stored full text exceeds 2,000,000 characters, the stored file and footer both mark that cap explicitly.

The stored full text is local plaintext cache data. It may contain the complete cleaned contents of extracted pages, persists until cleared, and currently has no automatic TTL or total-size eviction. Use `python3 search.py --cache-stats` to inspect `web_text_entries`, `web_text_size_bytes`, and the combined cache size; use `python3 search.py --clear-cache` to remove both normal JSON cache entries and `cache/web/*.md` full-text files while preserving provider-health state. For privacy-sensitive or throwaway extraction runs, set `WSP_CACHE_DIR` to a disposable directory or clear the cache afterward.

## Result quality

Search results pass through a quality layer before they reach the agent:

- **Latency memory:** every real provider call records latency, error, and empty-result outcomes (rolling window, last 50 calls / 7 days). The hedged fallback uses them to decide how long to wait for a slow provider before racing the next one, and the Operator Console shows them as provider health. They do not steer routing: the first provider comes from the query intent alone. The 4.x setting `auto_routing.adaptive_routing` is still accepted in `config.json` and has no effect.
- **Spam/mirror filter:** results from known Stack Overflow/GitHub content mirrors and SEO scrapers are removed (reported in `metadata.spam_filtered`). Extend via `quality.blocked_domains`, rescue a domain via `quality.allowed_domains`, or disable with `quality.filter_spam: false`.
- **Domain diversity:** at most 2 results per domain keep their position; overflow is moved behind the diverse head (`quality.max_results_per_domain`, `0` disables).

### Diversity Score

When quality reporting is enabled, `quality_report.diversity` explains result-set variety without changing the returned order. Its 0–1 score is a documented weighted blend: 40% registrable-domain diversity, 30% canonical-URL uniqueness, 20% snippet-content diversity, and 10% provider mix. Tracking parameters and fragments do not make URLs distinct; snippets are compared with casefolded word trigrams, and pairs at or above the configured threshold are reported as near duplicates. A single-provider set receives `provider_mix: 1.0`, because provider mix is only meaningful after a research merge.

The default is diagnostic-only. To let Research Mode stably move URL/content duplicate candidates behind diverse results (never as a one-off removal), opt in with:

```json
{
  "quality": {
    "diversity": {
      "rerank": true,
      "near_duplicate_threshold": 0.6
    }
  }
}
```

`near_duplicate_threshold` must be between `0.0` and `1.0`; higher values require more overlap. With `rerank: false` (the default), research merge ordering and deduplication behavior are unchanged.
- **Explicit intent wins:** queries with `site:` operators or `include_domains` are exempt — constrained domains bypass the spam filter and the diversity rerank is skipped entirely.

## Reliability and cost controls

The plugin is designed to fail visibly rather than invent confidence.

- Search result cache TTL is 1 hour by default. Recency queries and `freshness`/`time_range` cap that automatically: live/hour 60s, latest/day 300s, week 1800s. Queries the router files under live sports, security advisories or company earnings are capped at 300s as well.
- Searches that differ only in letter case, spacing or Unicode form ("Best  NAS 2026" and "best nas 2026") share one cache entry.
- Cache files and provider health state live under `WSP_CACHE_DIR`, or the plugin cache directory if unset.
- Use `no_cache=true` on `web_search_plus`, or `--no-cache` in CLI tests, when you need a fresh provider call. Cached hits include age; recency queries are labeled.
- Transient provider errors are retried with short backoff.
- Repeated provider failures put that provider on cooldown, stepping from 1 minute to 5 minutes to 25 minutes to 1 hour.
- Research mode harvests providers in completion order but keeps the public result order deterministic. By default it may stop waiting after at least two providers contribute a sufficiently diverse result head; every provider skipped by this optimization remains visible as `preempted_after_quorum` in routing diagnostics. Tune or disable this under `quality.research_quorum` (`enabled`, `min_contributing_providers`, `result_target_cap`, `min_unique_domains`). This behavior is implemented for WSP's own provider, budget, provenance, and receipt contracts; it does not make heterogeneous providers equivalent.
- `research_time_budget` remains a best-effort wall-clock bound, not a provider-side billing limit.
- Missing extraction keys, empty results, quota failures, and budget exhaustion are returned as warnings or metadata where possible.

The plugin cannot normalize or guarantee provider pricing. Provider APIs own their own billing, rate limits, index freshness, and terms.

## Updating

Update the plugin with Hermes’ plugin workflow or by pulling the installed clone, then restart Hermes:

```bash
cd ~/.hermes/plugins/web-search-plus
git pull
python3 -m pytest -q
python3 -m compileall -q __init__.py search.py setup.py scripts tests
```

Check [CHANGELOG.md](../CHANGELOG.md) before upgrading across feature releases.

## More help

- [FAQ](FAQ.md) for common setup and routing problems.
- [Architecture](ARCHITECTURE.md) for routing, trust boundaries, caching, and provider extension notes.
