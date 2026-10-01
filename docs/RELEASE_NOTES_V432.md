# Web Search Plus 4.3.2

A security release. Upgrading is recommended for everyone.

## Fixed

- **Extract URL validation.** Target URLs that Python and a browser-grade parser could read differently are rejected: backslashes, whitespace and control characters, userinfo, percent-escaped or non-ASCII hosts, and ambiguous numeric IPv4 such as `0177.0.0.1` or `127.1`. Literal IPs, including IPv4-mapped, IPv4-compatible, NAT64 and site-local IPv6, and every DNS answer are checked for non-global ranges.
- **Credential forwarding on redirects.** The shared HTTP client follows only exact same-origin redirects (scheme, host, effective port) on the pooled and urllib paths, so API keys never reach another origin. The urllib opener handles http(s) only.
- **Option injection.** Option-like values behind variadic CLI flags are rejected before a child process starts.
- **Response size.** Wire and decoded response sizes are limited to 16 MiB, with bounded gzip/deflate decoding.
- **Provider error text.** Text supplied by a provider in an error is no longer passed to the model. Errors carry a fixed message and the status code.
- **DonSeTch environment.** The child process receives an allowlisted environment.
- **Receipts lock.** A transient ENOENT while creating the lock file is retried a bounded number of times.
- **Dependencies.** `fast-uri` 3.1.8 in the schema test tooling.

## Added

- Search and extract output starts with a notice that returned titles, snippets and page text are untrusted web data.

## Behavior changes

- Redirects that change host, port or scheme fail, including http to https and apex to www.
- Provider error messages are less detailed.
- Extract rejects non-punycode IDN hosts, userinfo and short numeric IPv4 forms.
- Other DonSeTch environment variables are not inherited.

## Residual risk

The URL check runs before dispatch and is not a network boundary. DNS rebinding and redirects followed by the final fetcher need an egress policy on that fetcher; see the User Guide.
