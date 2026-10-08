# Web Search Plus 4.3.5

Two fixes. No routing, provider or config format changes.

## Fixed

- **Dedup no longer merges different pages on the same path.** `youtube.com/watch?v=A` and `youtube.com/watch?v=B` were treated as one result because every query parameter was dropped. Now only tracking parameters are removed: `utm_*` and the names in `TRACKING_PARAMETER_NAMES` (`fbclid`, `gclid`, `ref` and others).
- **Missing-key guidance reaches v3 consumers.** The v3 error classification replaces provider error text with a fixed message so upstream text cannot leak. That also replaced WSP's own missing-key hint. The hint is now kept when it has the exact shape `config.validate_api_key` produces: message `Missing API key for <provider>` or `Missing SearXNG instance URL`, plus `details.env_var`, `details.how_to_fix` and `details.setup_required`. Any other text stays redacted.
- **Extract without a key** returns the same setup guidance as search.

## Compatibility

`ErrorV3.details` may now be non-empty for configuration errors. The `code` (`wsp.config.provider_invalid`) and `error_class` (`config`) are unchanged.
