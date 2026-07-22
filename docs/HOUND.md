# Hound local MCP provider

Web Search Plus 3.2 can use [Hound](https://github.com/dondai1234/master-fetch)
as an optional local provider for both search and extraction. Hound is an
independent MIT-licensed project created and maintained by
[Bishesh Bhandari](https://github.com/dondai1234). Web Search Plus does not
bundle, fork, or modify Hound; it connects to a separately installed Hound
process over MCP Streamable HTTP on loopback.

The integration is explicit-only by default. Installing Hound does not add it
to automatic routing or fallback.

## What "keyless" means

Hound does not require a commercial search or extraction API key, account, or
per-request payment. Search requests are sent directly from your machine to
public search engines, and extraction requests are sent directly to target
sites.

Keyless does **not** mean offline, anonymous, or free of operating cost:

- your public IP is visible to search engines and target sites;
- public engines can rate-limit, block, or change behavior;
- browser-backed extraction uses local CPU, memory, storage, and Chromium;
- latency and result quality vary, and there is no hosted-service SLA;
- website terms and local law still apply to what you retrieve.

For those reasons Hound is a controlled local fallback, not a silent default.

## Requirements

- Web Search Plus 3.2 or newer
- Python 3.11 or newer for Hound
- a Hermes runtime with the MCP Streamable HTTP client and `httpx`
- loopback access to the Hound process
- optional Chromium installation for browser-backed extraction

The integration is tested against `hound-mcp==11.1.6`. Newer compatible Hound
releases should work through the same public MCP tools, but pinning the tested
version gives the most reproducible setup.

## Install Hound separately

A dedicated virtual environment keeps Hound's browser, PDF, OCR, and search
dependencies out of the Hermes plugin environment:

```bash
python3.11 -m venv ~/.local/share/hound-wsp/venv
~/.local/share/hound-wsp/venv/bin/python -m pip install \
  "hound-mcp[all]==11.1.6"
~/.local/share/hound-wsp/venv/bin/playwright install chromium
```

For HTTP-only fetching without the browser/PDF/OCR extras, install
`hound-mcp==11.1.6` instead. See Hound's upstream documentation for the exact
capability differences and supported platforms.

## Start the loopback sidecar

```bash
~/.local/share/hound-wsp/venv/bin/hound \
  --http \
  --host 127.0.0.1 \
  --port 8765 \
  --cache-ttl 0
```

Keep that process running while Hermes uses Hound. For persistent deployments,
manage it with your normal local service manager and a dedicated unprivileged
user. Do not expose the port on a public interface.

Web Search Plus accepts only uncredentialed HTTP loopback endpoints using
`127.0.0.1` or `::1`. Hostnames, remote addresses, URL userinfo, query strings,
and fragments are rejected. The default endpoint is:

```text
http://127.0.0.1:8765/mcp
```

## Configure Hermes

Set the endpoint in the environment used to launch Hermes or its gateway:

```bash
export HOUND_MCP_URL=http://127.0.0.1:8765/mcp
```

Then restart or reload that Hermes process so the optional environment value is
visible to the plugin.

`HOUND_MCP_URL` is an endpoint, not an API credential. It remains a readiness
requirement because Hound is unavailable when the local process is not running.
The equivalent plugin config section is:

```json
{
  "hound": {
    "endpoint": "http://127.0.0.1:8765/mcp",
    "timeout_seconds": 120,
    "max_content_chars": 60000
  }
}
```

## Verify

Check Hound itself:

```bash
~/.local/share/hound-wsp/venv/bin/hound --doctor
```

Then make explicit Web Search Plus calls:

```python
web_search_plus(
    query="Python programming language official website",
    provider="hound",
    count=3,
)

web_extract_plus(
    urls=["https://example.com"],
    provider="hound",
)
```

Successful responses identify `hound` as the selected provider. If the sidecar
is absent or times out, Web Search Plus returns a typed provider error rather
than inventing results.

## Routing, caching, and privacy

- Hound defaults to `auto_allow=false`; explicit calls work immediately, while
  automatic routing and fallback remain unchanged.
- Web Search Plus owns routing, freshness reporting, normalization, receipts,
  SSRF checks, bounded output, and its evidence cache.
- The adapter requests `cache_ttl=0` from Hound so the sidecar cache does not
  become a second authoritative state layer.
- Search maps to Hound's `smart_search`; extraction maps to `smart_fetch`.
- Hound may use plain HTTP, a local browser, PDF tooling, or OCR depending on the
  target and installed extras.
- Queries and URLs are not sent to a commercial Hound service, but they still
  leave your machine for public search engines and destination websites.

To opt Hound into automatic routing deliberately:

```bash
python3 ~/.hermes/plugins/web-search-plus/setup.py \
  config set-auto-allow hound on
```

Do this only after measuring local reliability and latency. Explicit-only is
the recommended default.

## Attribution and licenses

- Hound / `hound-mcp`: Copyright Bishesh Bhandari, MIT License
- Hound repository: https://github.com/dondai1234/master-fetch
- Web Search Plus: separate MIT-licensed project and MCP client integration

Web Search Plus ships only its independent adapter. Hound's package, code,
models, browser dependencies, and their licenses remain part of the separate
Hound installation.
