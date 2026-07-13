# WS-3 Task 5 Gate — Static Operator Console Frontend

**Status:** PASS · EXTERNAL OPUS CLOSEOUT CONFIRMED
**Date:** 2026-07-12
**Branch:** `feat/v3-evidence-spine`
**Base commit:** `29b4981`

## Delivered

- Fable's `index.html`, `styles.css`, and `app.js` are stored byte-for-byte under `web/v3/console/`.
- `ui.py` serves exactly `/`, `/styles.css`, and `/app.js` with fixed MIME types.
- Static assets are loaded once at startup through a bounded `O_NOFOLLOW` path and retained in memory.
- Static root, asset, and ancestor symlinks fail closed.
- Static responses use the Console CSP:

  `default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`

- API and error responses retain the stricter `default-src 'none'` policy.
- Existing loopback bind, strict Host validation, Bearer authentication, no-store, no-CORS, no-request-log, and GET/HEAD-only enforcement remain active.

## Browser authentication bridge

The supplied `app.js` is intentionally unchanged and its same-origin `fetch()` calls do not attach a Bearer header. A normal browser cannot provide that header during navigation. The server therefore accepts one authenticated bootstrap request at `/?token=...`, compares the token with `hmac.compare_digest`, sets an opaque HMAC-derived `wsp_console_session` cookie (`HttpOnly; SameSite=Strict; Path=/`), and returns `303 Location: /`.

The raw startup token is never stored in the cookie, response body, server logs, referrer, or final page URL. Bearer authentication remains supported for API clients. The residual boundary is explicit: the initial loopback bootstrap URL may remain in local browser history. External closeout must decide whether this is acceptable for the local-only 3.0 Console or whether the browser-auth handshake must change.

## Asset integrity

- `index.html`: `c3e0e498ced835032c20ee2c7fed319e344fceb7fb5bc07c3449fc22e0c293d0`
- `styles.css`: `df7f2e531f3296fb8a3b453de39fbddf44211198b736aa707650bf1acc98a5f0`
- `app.js`: `4daab257bdccd433d7a914aeafd15cd6bde10e2ab2fb143292a10c00cddfb62c`

All three hashes match the supplied attachments. `node --check` passes. Static checks confirm no inline script/style/event handlers and no mutating fetch method.

## Real Chromium gate

Chromium `149.0.7827.55` ran against a disposable fixture cache through the real server and real snapshot builders.

Verified:

- final URL is token-free: `http://127.0.0.1:38765/`;
- session cookie is HttpOnly, SameSite Strict, path `/`, and inaccessible through `document.cookie`;
- Overview rendered contract `3.0`, plugin `3.0.0-dev`, 12 providers, no error banner;
- Routing Receipts rendered 2 records, 2 current-execution panels, and exactly 1 visually separate cache-origin panel;
- Benchmark History rendered 1 search run and explicit `extract: not_collected` copy (data gap, not zero);
- Refresh changed the update timestamp;
- limit selectors emitted actual `GET /api/v3/receipts?limit=10` and `GET /api/v3/benchmark-history?limit=5` requests;
- all 11 observed frontend API requests used GET;
- normal run had zero CSP/security-console errors, page errors, and request failures;
- an injected inline script remained unexecuted and produced a real CSP violation;
- `POST /` returned `405` with `Allow: GET, HEAD`;
- anonymous `GET /` returned `401`;
- listener was exactly `127.0.0.1:38765`, not wildcard/Tailnet;
- fixture cache hashes were unchanged after all browser interactions;
- 390×844 mobile run had no body overflow and no long-ID text rect outside a receipt;
- the intentionally scrollable tab strip moved from `scrollLeft=0` to `86`, and Benchmark History was reachable/rendered.

Browser evidence: `/tmp/wsp-ws3-browser-evidence.json`

Screenshots:

- `/tmp/wsp-ws3-task5-overview-desktop.png`
- `/tmp/wsp-ws3-task5-receipts-desktop.png`
- `/tmp/wsp-ws3-task5-receipts-mobile.png`

## Automated gates

- Full pytest: **745 passed + 6 subtests passed**
- Focused WS-3 frontend/backend/security: **35 passed**
- Ruff: PASS
- Python compileall: PASS
- JavaScript syntax: PASS
- Contract v3 schema boundary: PASS
- Contract v3 schema reproducibility: PASS
- Provider/routing docs reproducibility: PASS
- `git diff --check`: PASS

## External Opus closeout

Andy inspected the actual uncommitted diff and browser evidence read-only over SSH and returned:

> **WS-3 Task 5 external closeout: PASS**

No release blocker was found. The bootstrap browser-history exposure was explicitly accepted for the loopback-only, secret-free, read-only 3.0 Console. A random per-session cookie and optional one-time bootstrap invalidation are recorded as non-blocking 3.1 hardening, not a 3.0 requirement.

Audit evidence:

- conversation ID: `hermi-tg-topic-781`
- Andy session: `4dd63cc4-d532-444c-8ab4-a13ef2e95cd8`
- remote transcript existence/non-empty check: CONFIRMED
- verdict: PASS

Task 5 is complete. WS-3 itself is not declared complete here: the sequenced real Extract benchmark and Task 6 real-cache/browser closeout remain next. No push, merge, release, or plugin switch occurred.
