# Web Search Plus 5.1.10

Ready for Hermes v0.23. Settings with non-ASCII text survive any system locale, `status` stops recommending a setting Hermes does not have, and `config show` agrees with the new `hermes plugins config` editor. No configuration changes are needed; tools, hooks and permissions are unchanged, so the update applies like any patch.

## Fixed

- **UTF-8 everywhere** (thanks to @pompousman, #175). On Windows, and on any system with a non-UTF-8 locale, a valid `config.json` holding a character such as `ü` or `”` was moved aside and every setting fell back to the default. `config.json`, `.env`, the onboarding state and the Hermes `config.yaml` are now read as UTF-8 (a BOM is accepted); damaged bytes still move the file aside instead of being silently replaced. The command-line tools print UTF-8.
- **No more `never_defer` advice.** `status` suggested listing the Plus tools under `tools.tool_search.never_defer`. Hermes has no such setting: its tool search always defers plugin tools and never its own `web_search`. The hint now says what happens without `hermes config set web.search_backend wsp`: the model sees Hermes' `web_search` first and `web_search_plus` only through tool search.
- **`config show` shows what searches use.** Hermes v0.23.4's `hermes plugins config web-search-plus` (like Hermes Desktop) stores some settings in `config.yaml`, which searches use on top of `config.json`. `config set-routing on` printed "Updated" while searches kept routing off. `config show` and every `config set-*` now show the effective values, name the fields that come from the Hermes plugin settings, and say when those win.
- **Texts match the routing.** The `web_search_plus` description the model reads said "Serper for security and shopping" (Brave since 5.1.6). `hermes plugins config` now lists Brave, Serper, Exa and Linkup first, and explicit-only providers name `config set-auto-allow <provider> on` instead of "enable in config.json".

## Added

- `brave.api_url`, `tavily.search_url` and `exa.base_url` in `config.json` point search at a compatible proxy or gateway, like the existing endpoint overrides. Defaults are unchanged. Same engine change as web-search-plus-mcp 5.1.9 (contributed there by @hardworkDAD, #71).

## Checked before release

- Real Hermes v0.23.4 CLI in a throwaway home: `plugins validate`, enable, status, setup, keyless start, `hermes plugins config`, the native `wsp` backend and `plugins.isolation: host`.
- An independent test run on a separate installation with real provider calls (search, research, a 404 extract) and the full suite under a C locale.
- Full suite with and without a Hermes core on the path, also under `LC_ALL=C`: no failures.

Full list: [CHANGELOG](../CHANGELOG.md).
