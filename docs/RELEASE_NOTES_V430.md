# Web Search Plus 4.3.0

Minor release on the 4.x line. It adds Hermes Desktop settings and fixes adaptive routing, which had stopped learning. Existing configs keep working unchanged.

## Hermes Desktop settings

Hermes Desktop can now configure Web Search Plus from its plugin settings form. The form has 18 fields:

- five flat settings: country, language, max results, auto-routing, and SearXNG URL
- one API key field for each supported provider (13 in total)

Values saved under `plugins.entries.web-search-plus.settings` overlay `config.json` one way. API keys go to `.env` and never reach `config.json`. Empty, `0`, and `null` values leave the existing config unchanged. Priority, budgets, and other nested settings stay in `config.json`.

Without PyYAML, a simpler built-in parser reads the settings. It gives the same values as PyYAML for block maps, one-line `{key: value}` maps, unquoted `null`/`~`, and `yes`/`no`/`on`/`off`.

## Adaptive routing learns again

Since the 3.0 attempt engine, searches that the engine runs had stopped recording results. Adaptive routing received no new samples and fell back to static priority. The Operator Console provider-health view showed old data.

Every real provider call now records latency, result count, and error. This includes each research member and each retry. Cache hits, config errors, and bench runs still record nothing. The engine still handles retries and circuit state on its own.

`provider_stats.json` writes now take a file lock (POSIX). Before, two processes writing at the same time, such as the gateway and a CLI run, overwrote each other and lost up to half of the samples.

## `max_results` reaches the agent tool

When the agent omits `count`, the `web_search_plus` tool now uses `defaults.max_results`. Before, the tool always asked for 5, so the setting only affected the CLI. An explicit `count` still wins, and the value is clamped to 1–20.
