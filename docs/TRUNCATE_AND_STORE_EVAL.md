# Truncate-and-store output eval

Local eval for `web_extract_plus` output-layer truncation. This uses a direct fetch of a large public documentation page to isolate formatter behavior; provider fetch logic is intentionally unchanged.

- URL: `https://docs.python.org/3/library/asyncio-task.html`
- Config: `web.extract_char_limit = 15000`
- Previous inline output chars: `171862`
- New inline output chars: `13696`
- Reduction: `92.0%`
- Stored full-text chars: `171750`
- Stored path shape: `<WSP_CACHE_DIR>/web/8648139190e534786d6204dff7161d395d89e1688db509a72c2f161c4389e645.md`

The new output includes a head/tail preview plus a concrete `read_file(path=..., offset=..., limit=500)` footer for page-on-demand access to the omitted middle.
