# Web Search Plus 4.3.1

Performance release. No config or API changes.

## Faster provider calls

Provider HTTP calls reuse keep-alive connections per host, so repeated searches to the same provider skip the TCP and TLS handshake. Live medians with real keys, same process:

- Serper: 1.11 s → 0.97 s (−12 %)
- Tavily: 1.79 s → 1.49 s (−17 %)
- Exa: −6 %

The first call per provider is unchanged. Proxies, non-HTTP(S) URLs, and `WSP_HTTP_KEEPALIVE=0` keep the previous `urlopen` path. A stale pooled connection is retried once on a fresh socket, and the pool is dropped after `fork()`. Error mapping, redirects, gzip, and timeouts behave as before.

The MCP companion `web-search-plus-mcp 4.3.1` additionally runs searches inside the server process instead of spawning one Python process per call (auto routing 1.70 s → 0.99 s).

See the [Changelog](../CHANGELOG.md).
