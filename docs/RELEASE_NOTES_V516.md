# Web Search Plus 5.1.6

Better sources. These fixes come from testing what the model actually receives, with real keys and real clients. No configuration changes are needed.

## Fixed

- **No more Google `goto` links as sources.** Since Google's mid-2026 change, Serper returns `google.com/goto?url=…` instead of the real address for every result from position 4 on. The token cannot be decoded, so a model cited them as sources and users got useless links. Web Search Plus now drops them before ranking and caching, without contacting Google. A Serper search therefore keeps about three sources. The header shows `redirects_dropped=N`. Optional `"serper": {"resolve_redirects": true}` tries to resolve them with one request per link to Google. In tests Google redirected only the first link of a search, so this is off by default. Classic `google.com/url?q=` links are decoded locally.
- **SerpBase returns working links.** Web Search Plus did not send SerpBase's `device` parameter, and SerpBase then rebuilt links from Google's shortened desktop breadcrumbs. In one test 4 of 8 links were 404. It now asks for mobile results (`serpbase.device`, default `mobile`): no 404s in four test searches.
- **Broken URLs are filtered.** Result URLs with control characters, replacement characters or mojibake are dropped for every provider, for example a Querit result ending in `?2002zzp&1712aznkjÂ`. Legitimate non-ASCII addresses such as `/wiki/Zürich` stay. The header shows `invalid_urls_dropped=N` when that happens.

## Changed

- **Security and shopping searches start with Brave.** They used to prefer Serper, which now returns about three real sources per search, and whose shopping links point to Google Shopping instead of the merchant. Serper stays as a fallback and remains a good choice for page extraction.
- **DonSeTch 4.10.3** is the tested version (4.7.0 and newer work).
- Tests now guard the size of the text the model receives per search and extract.

Full list: [CHANGELOG](../CHANGELOG.md).
