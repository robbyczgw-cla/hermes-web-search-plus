# Web Search Plus 3.4 — Release Notes

Web Search Plus 3.4 adds Octen as an optional source-search provider through
Monid. Octen is off by default and runs only when explicitly selected; existing
provider configuration and automatic routing remain unchanged.

## Optional Octen source search via Monid

Set `MONID_API_KEY` from [Monid](https://app.monid.ai/access/api-keys), then
request the provider explicitly:

```python
web_search_plus(
    query="recent vector database research",
    provider="octen",
    freshness="month",
)
```

The adapter calls Octen's `/search` endpoint through Monid's documented API and
returns ranked source links and highlights. It supports canonical freshness and
include/exclude-domain filters.

The integration deliberately does **not** call Octen's answer, Broad Search,
image/video, or full-content modes. `full_content` is disabled, and Octen never
enters automatic routing or fallback unless an operator deliberately changes
its `auto_allow` setting.

## Access, billing, and limits

Octen supplies the search results; Monid provides API access and billing.
Access and billing use Monid's prepaid wallet; see Monid for current pricing and
terms. No free-tier claim is made: a Monid API key and wallet balance are
required.

## Security and failure handling

- Credentials are sent only to Monid's fixed HTTPS API origin.
- Redirects are rejected so credentials cannot cross origins.
- Response bodies are capped at 8 MiB.
- Upstream failures are sanitized and classified without exposing credentials
  or raw private payloads.
- Monid lifecycle failures remain distinguishable from Octen provider HTTP
  failures.

## Provider SDK correctness

Discovered Provider SDK search providers now honor
`ProviderSpec.supports_freshness`. When a provider successfully applies a
canonical freshness filter, the public metadata no longer reports that filter
as unsupported.

## Documentation since 3.3

The README opening now explains the product in plain user language, and the
repository includes a dedicated contribution guide covering setup, source-only
provider contracts, Provider SDK intake, generated-artifact checks, security
reporting, and maintainer-only release boundaries.

## Compatibility

- No Hermes tool is renamed or removed.
- Existing provider keys and automatic-routing defaults remain valid.
- Octen is additive, optional, explicit-only, and source-only.
- Existing result fields remain stable.

## Release inventory

- [#110](https://github.com/robbyczgw-cla/hermes-web-search-plus/pull/110) — plain-language README introduction.
- [#112](https://github.com/robbyczgw-cla/hermes-web-search-plus/pull/112) — repository contribution guide.
- [#113](https://github.com/robbyczgw-cla/hermes-web-search-plus/pull/113) — explicit Octen source search via Monid.

## Upgrade

```bash
hermes plugins update web-search-plus
```

Reload Hermes after updating so the registered plugin tools use the new code.
