# DonSeTch local provider

Web Search Plus 4.0 can use [DonSeTch](https://github.com/dondai44423/donsetch)
2.3.1 as a separately installed local provider for source-only Search and
Extract. DonSeTch is an independent AGPL-3.0-only project. Web Search Plus
does not bundle, copy, or redistribute its binary.

The WSP adapter starts the configured DonSeTch executable as a local stdio MCP
process and calls its `web_search` and `web_fetch` tools. Responses are
normalized into the normal WSP source-only result envelopes. DonSeTch remains
explicit-only by default; installing it does not silently change automatic
routing or fallback.

## Install DonSeTch separately

The upstream project distributes a platform binary through npm:

```bash
npm install -g donsetch@2.3.1
command -v donsetch
donsetch --version
donsetch doctor
```

The executable must be available to the Hermes process, not only to an interactive shell.

For Linux browser escalation, DonSeTch may require a usable Chromium/Chrome
installation and Xvfb. The WSP integration has not been declared browser-
verified on Linux hosts without that infrastructure.

## Configure Hermes

Set the absolute executable path in the active Hermes environment file or
process environment. Do not put API keys in the WSP repository:

```bash
export DONSETCH_BIN="$(command -v donsetch)"
```

For a persistent Hermes setup, add the resulting absolute path to
`~/.hermes/.env` and restart the Hermes process that loads provider
configuration. `DONSETCH_BIN` is a path, not a network URL or API credential.

## Use DonSeTch explicitly

```python
web_search_plus(
    query="Hermes Agent documentation",
    provider="donsetch",
    count=5,
)

web_extract_plus(
    urls=["https://example.com"],
    provider="donsetch",
)
```

The adapter supports source search and Markdown extraction. It forwards
browser escalation requests through DonSeTch's tier selection, but WSP does not
claim that every anti-bot or JavaScript path succeeds on every host. Raw HTML
and non-Markdown output are not provided by this adapter; use another WSP
extract provider when those are required.

To deliberately allow DonSeTch in automatic routing after measuring it locally:

```bash
python ~/.hermes/plugins/web-search-plus/setup.py config set-auto-allow donsetch on
```

To return it to explicit-only mode:

```bash
python ~/.hermes/plugins/web-search-plus/setup.py config set-auto-allow donsetch off
```

## Migration from Hound

Web Search Plus 4.0 removes the Hound provider. Remove Hound-specific settings
and replace them with the DonSeTch executable path:

```bash
unset HOUND_MCP_URL
export DONSETCH_BIN="$(command -v donsetch)"
```

Change `provider="hound"` to `provider="donsetch"`. A Hound HTTP sidecar is no
longer read by WSP, and `HOUND_MCP_URL` has no effect in version 4.0.

## Boundaries and attribution

- DonSeTch is installed and operated separately from Web Search Plus.
- DonSeTch's AGPL-3.0-only license remains applicable to that independent
  component; review its terms for your deployment.
- Search queries and fetched URLs still reach public search engines or target
  sites. Local control-plane execution does not mean offline or anonymous.
- WSP owns the routing, normalization, error envelope, cache, and receipt
  boundary; DonSeTch owns its local retrieval engine and runtime state.
- In the standalone MCP package, explicit DonSeTch calls get a 195-second outer
  subprocess budget, covering the adapter's 180-second stdio timeout; other
  provider budgets remain unchanged.
- This integration was tested against DonSeTch 2.3.1 for stdio MCP
  initialization, Search, Fetch, and structured error handling. Successful
  browser/anti-bot execution remains an environment-dependent open gate.
