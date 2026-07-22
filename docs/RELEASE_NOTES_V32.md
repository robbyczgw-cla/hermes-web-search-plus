# Web Search Plus 3.2 — Release Notes

Web Search Plus 3.2 adds an optional local Hound provider for source-only search
and extraction. The public tool names and default routing policy remain
unchanged. Hound is explicit-only unless an operator deliberately opts it into
automatic routing.

## Hound: local search and extraction over MCP

[Hound](https://github.com/dondai1234/master-fetch) is an independent,
MIT-licensed web-research MCP server created and maintained by
[Bishesh Bhandari](https://github.com/dondai1234). Web Search Plus does not
bundle or fork Hound. The new provider connects to a separately installed Hound
sidecar over MCP Streamable HTTP on loopback.

The integration maps:

- `web_search_plus(provider="hound")` to Hound `smart_search`;
- `web_extract_plus(provider="hound")` to Hound `smart_fetch`.

Web Search Plus continues to own routing, source-only projection, SSRF policy,
freshness reporting, bounded output, evidence caching, and execution receipts.
Hound's request cache is disabled by the adapter so it does not become a second
authoritative cache layer.

The integration is tested against `hound-mcp==11.1.6`.

## Keyless, with the costs stated plainly

Hound needs no commercial search API key, vendor account, or per-request
payment. Search and extraction run from the operator's machine.

That does not make the path offline or anonymous. Public search engines and
target sites receive requests from the machine's IP, may rate-limit or block
traffic, and provide no SLA. Browser-backed extraction can require Chromium and
meaningful local CPU, memory, and storage. Search and difficult browser fetches
can be slower than hosted APIs.

Hound therefore defaults to explicit-only. Existing automatic routing and
fallback behavior are unchanged after upgrading to 3.2.

## Security boundary

The configured Hound endpoint must be uncredentialed HTTP on `127.0.0.1` or
`::1`. Remote addresses, hostnames, URL userinfo, query strings, and fragments
are rejected. The client ignores proxy environment variables, refuses HTTP
redirects, bounds response size, and maps transport, MCP, timeout, and malformed
payload failures to typed provider errors.

The endpoint is configured with:

```bash
export HOUND_MCP_URL=http://127.0.0.1:8765/mcp
```

See [Hound local MCP provider](HOUND.md) for installation, verification,
operating costs, privacy boundaries, and the deliberate auto-routing opt-in.

## Provider surface

WSP 3.2 exposes:

- 13 search-capable providers;
- 9 extraction-capable providers;
- the same two public Hermes tools;
- no new default automatic provider.

## Compatibility

- Existing provider keys and calls continue to work unchanged.
- Hound requires a separate Python 3.11+ installation and running loopback
  sidecar; it is not installed by the Hermes plugin.
- The core provider remains dormant when `HOUND_MCP_URL` is absent.
- Explicit Hound calls fail with typed diagnostics when the endpoint or required
  MCP client runtime is unavailable.
- Operators who do not install Hound observe no routing or dependency change.

## Credits and upstream collaboration

Hound is created and maintained by Bishesh Bhandari (`dondai1234`). Web Search
Plus is grateful for the project, the open MCP interface, and the constructive
upstream collaboration around search-domain validation, canonical URL
normalization, and content classification.

Related upstream work:

- https://github.com/dondai1234/master-fetch/pull/7
- https://github.com/dondai1234/master-fetch/pull/8
- https://github.com/dondai1234/master-fetch/pull/9
- https://github.com/dondai1234/master-fetch/pull/10

The first three changes were accepted into Hound upstream (PR #8's change was
integrated manually); PR #10 remained open when these notes were prepared.
These contributions improve Hound itself and are separate from the WSP adapter.
