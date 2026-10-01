# Web Search Plus 4.3.3

A small follow-up to 4.3.2.

## Fixed

- **Internationalized hostnames in extract.** 4.3.2 rejected URLs such as `https://müller.de`. They are now converted to punycode (`xn--mller-kva.de`, IDNA 2008, as browsers do), and the converted URL is what gets validated and passed to the fetcher, so the check and the fetch read the same host. Labels that mix scripts, use compatibility characters such as fullwidth letters or ideographic full stops, or are not valid IDNA are still rejected. The conversion uses the optional `idna` package when it is installed (IDNA 2008, like browsers). Without it the standard library codec is used, and the few characters where the two standards differ (`ß`, final sigma, zero-width joiners) are rejected rather than mapped to a different site. The plugin keeps no required dependencies.

## Clarified

- The same-origin redirect rule from 4.3.2 applies to the HTTP client that calls provider APIs (provider API calls, not the pages you extract). Extracting `http://example.at` or a page that redirects from apex to `www` is not affected; the provider fetches those pages.
