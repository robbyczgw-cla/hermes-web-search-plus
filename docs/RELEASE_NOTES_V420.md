# Web Search Plus 4.2.0

Optional Jev (TypeSafe System One) is available and **off by default**. It is not a search provider.

## Enable

```bash
python3 ~/.hermes/plugins/web-search-plus/setup.py setup --preset lean \\
  --jev --jev-key-file /path/to/typesafe_api_key
```

Interactive setup asks `[y/N]` and defaults to no. Store the TypeSafe key in a file (`TYPESAFE_API_KEY_FILE`, mode 600). Do not put the key in `config.json`.

Leave Jev off with `--no-jev` or by skipping the prompt.

## What it does

When `jev.enabled` is true, three optional decisions can run:

1. **search_type overlay** — Keyword heuristics may propose `news`. Jev confirms only at confidence ≥ 0.95. Otherwise the request stays `search`. Explicit `search_type=news` is not overridden. WSP still exposes only `search|news`; shopping/local are not new verticals.
2. **extract_quality** — After extract, Jev may keep a long page that regex treated as a bot wall, or reject chrome, if confidence ≥ `jev.min_confidence` (default 0.85). Regex still rejects short Cloudflare/CAPTCHA pages without calling Jev.
3. **language_fill** — Only when WSP language inference returned none. Jev may fill `de`/`en`/… at high confidence. It does not override CLI, config, or a successful inference.

Missing SDK, missing key, timeout, or low confidence leaves WSP behavior unchanged.

## Config

```json
{
  "jev": {
    "enabled": false,
    "search_type": false,
    "extract_quality": false,
    "language_fill": false,
    "min_confidence": 0.85,
    "search_type_min_confidence": 0.95,
    "timeout_s": 8.0
  }
}
```

`setup --jev` turns those three decisions on. Narrow with `--jev-decisions search_type`.

`python3 ~/.hermes/plugins/web-search-plus/setup.py status` reports `enabled`, `key_present`, and `key_source` without printing the secret.

Install `typesafe-sdk` separately if you enable Jev. The plugin stays stdlib-only.

## Also in 4.2.0

Import on Windows when `fcntl` is missing. POSIX lock and journal paths still fail closed ([#129](https://github.com/robbyczgw-cla/hermes-web-search-plus/pull/129)).
