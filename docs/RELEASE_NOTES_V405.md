# Web Search Plus 4.0.5

## DonSeTch 3.6.1 compatibility

- Read search titles and snippets from the compact text evidence, matched to the original result rank and URL or handle before domain filtering.
- Recover search diagnostics and fetch title, status, quality, and site from the DonSeTch namespaced metadata. Do not expose the raw debug envelope.
- Continue accepting pre-compact structured responses.
- Pin the child transport to stdio even when the parent environment requests HTTP; leave the parent environment unchanged.

DonSeTch remains a separately installed optional provider. Search and extraction stay source-only. No WSP tool-schema migration or automatic backend selection is introduced.
