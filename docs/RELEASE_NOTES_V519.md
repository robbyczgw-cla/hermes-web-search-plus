# Web Search Plus 5.1.9

Settings mean what they say. A provider you allow for automatic search is used, `status` names a placeholder key and says when nothing runs automatically, and a self-hosted SearXNG URL set the Hermes way is picked up. Found by an audit of the MCP server against this plugin and decided with the maintainer; checked with the real Hermes v0.22.5 CLI in a throwaway home. No configuration changes are needed.

## Fixed

- **`config set-auto-allow <provider> on` is enough.** It only wrote the flag. Automatic search tries the providers in `provider_priority`, and DonSeTch, Octen and TinyFish are not in it, so the search still went to the Serper fallback while `status` said "ready". The provider is now added to the end of the order when the config is read; the order by query type stays, and `off` takes it out again.
- **`status` names a placeholder key.** `BRAVE_API_KEY=your_brave_key` in the profile showed as "not configured" with "setup brave". It now says the key is a template placeholder, with `hermes config set BRAVE_API_KEY <real value>`.
- **`status` says when no provider runs automatically.** With only an explicit-only or disabled provider, the dashboard showed "✓ search" and the first search without `provider="name"` failed. It now marks search `~`, lists such providers under `Only with provider="name"`, and prints the fix.
- **Self-hosted profile with `hermes config set SEARXNG_INSTANCE_URL`.** Status said SearXNG was ready, yet every automatic search failed with "self_hosted profile requires searxng.base_url". The profile check now uses the same readiness answer.
- **`auto_routing.enabled: 0`** (or `"false"`, `"off"`) turns automatic routing off; only the literal `false` did.
- **A damaged config.json or cache file** no longer fails every call: a number too large for a float (or nesting too deep) moves the config aside like any other invalid config, and a damaged search-cache file counts as a miss.
- **Operator console** Auto and Cooldown columns show what routing and admission actually do.
- `jev.api_key_file` in config.json is named once in the log (it is not read).

## Internal

Fewer, deeper modules with smaller interfaces (readiness, extract, native backend, DonSeTch adapter); each provider try now has one lifecycle that settles its budget and probe lease. The engine files stay identical to web-search-plus-mcp 5.1.8, which ships the same engine changes the same day. A differential test of the new provider-try engine against the old one over 12,000 generated scenarios found no difference.

## Checked before release

- Separate test installation (Python 3.14): all scenarios pass, including DonSeTch joining automatic search after the single `set-auto-allow` command, and a real search, research call and 404 extract.
- Each fix has a test that fails without it; full suite green (the one red test needs the Node module `ajv`, not installed locally).

Full list: [CHANGELOG](../CHANGELOG.md).
