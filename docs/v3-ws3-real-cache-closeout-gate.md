# WS-3 Real Extract + Copied-Real-Cache Closeout Gate

**Status:** LOCAL PASS — REAL EXTRACT, COPIED REAL CACHE, MOBILE + DESKTOP VISUAL GATES
**Date:** 2026-07-13
**Branch:** `feat/v3-evidence-spine`
**Task-5 base checkpoint:** `1370674`

## Verdict

The real Extract benchmark, typed Console history, copied-real-cache read-only behavior, desktop browser behavior, security gates, and full automated suite pass.

Canonical Revision 2 resolves the table defect while preserving the supplied `index.html` and `app.js`: both the Overview provider table and Benchmark History provider table collapse into labeled cards at narrow widths. Median latency and Errors are visible without table or document horizontal scrolling.

A user-authorized five-line CSS-only follow-up resolves the remaining navigation defect without changing markup, JavaScript, content hierarchy, colors, or desktop layout. At 390 px the active `Benchmark History` tab now ends at `right=367` inside a `375` px document client width; at the stricter 320 px viewport it ends at `right=297` inside a `305` px client width. The tab strip has no internal horizontal overflow at either width.

WS-3 therefore has a verified local closeout PASS. Independent external final closeout remains pending against the committed HEAD.

## Delivered: real Extract benchmark collection

- New explicit CLI command: `python3 search.py extract-bench`.
- Requires one or more `--extract-urls`; it never runs as a side effect of normal extraction or Console rendering.
- Calls each selected `EXTRACT_DISPATCH` adapter directly.
- Does not use fallback, retries, cooldown/health writes, adaptive provider stats, response cache, v3 state, or config writes.
- `--bench-providers` bounds provider spend; `--bench-timeout-budget` bounds the run; `--no-history` opts out of persistence.
- One provider failure cannot abort the remaining providers.
- Reports retain aggregate counts, latency, character volume, score, and safe error codes only.
- Target URLs, extracted content, credentials, provider error prose, headers, and paths are never retained.
- Completed summaries are persisted as marker-owned `kind="extract"` records in `operator/v3/benchmark-history.jsonl`.
- The existing Console reader projects the collected run as `extract: collected`.
- Journal storage is bounded, `0600`, locked, atomic-replace based, marker-owned, and refuses symlinked/corrupt/foreign storage rather than overwriting it.

## Live benchmark evidence

Scope was deliberately limited to **one public project page × three configured direct providers**: three provider calls total, no retries or fallback.

Results:

- `linkup`: PASS — 1/1, 100% success, 1.176 s, 24,397 returned characters, score 0.992;
- `tavily`: FAIL — 0/1, one safely classified provider error;
- `exa`: FAIL — 0/1, one safely classified provider error;
- run status: `ok=true` because real extraction evidence was collected successfully;
- typed history write: PASS;
- Console availability: `extract=collected`;
- automatic config/priority application: none.

The live run predated the later safe Auth/Quota/Timeout/HTTP error-code refinement, so its retained compact history contains only aggregate error counts. No extra live calls were spent merely to improve labels.

Evidence:

- `/tmp/wsp-ws3-live-extract-bench.json`
- `/tmp/wsp-ws3-live-extract-summary.json`
- summary SHA-256: `32745949d4a67a02d0032134ded1f25a7a8fe81b9bb2930410ae06e6c55f1e82`

## Side-effect gates

### Live benchmark against normal cache root

The complete non-history cache/state tree was hashed before and after the live benchmark.

- files compared: 5;
- non-history additions/modifications/deletions: 0;
- only the explicitly owned benchmark history and lock were excluded as expected writes.

### Hermetic real-shaped regression

`tests/test_ws3_real_cache.py` covers:

- provider health, provider stats, usage events, and legacy foreign JSON;
- owned, foreign, and corrupt v3 response entries;
- owned, foreign, and corrupt full-text files;
- existing SQLite circuit state;
- typed Extract benchmark history;
- real `ThreadingHTTPServer` API requests;
- recursive privacy serialization;
- exact before/after tree fingerprint equality.

Result: PASS.

### Copied actual cache + browser

The actual `/root/work/.cache` tree was copied to disposable storage and used by the real Console server.

- files compared: 7;
- post-browser additions/modifications/deletions: 0;
- provider/config calls from rendering: 0;
- original normal cache was not used by the browser server.

Result: PASS.

## Desktop browser gate

Real Chromium on a Tailnet-connected desktop accessed a temporary preview proxy. The product server remained bound to `127.0.0.1`; the preview proxy existed only under `/tmp`, bound only to the test host's Tailscale IP, accepted only the known browser source IP, rewrote `Host`, injected the disposable token, allowed only GET/HEAD, and was stopped immediately after evidence collection.

At 1440×900:

- Overview, Routing Receipts, and Benchmark History loaded;
- real Extract history rendered with Linkup/Tavily/Exa rows;
- Refresh completed;
- all observed `/api/v3/*` responses were HTTP 200;
- no page errors, unexpected console errors, request failures, token leakage, or absolute-path leakage;
- document width stayed within the viewport;
- injected inline script remained unexecuted under CSP.

Visual verdict: PASS.

Screenshot: `/tmp/wsp-ws3-final-1440-benchmarks.png`
SHA-256: `09a80497598b6afc62652a2cd096a4c8273c035b3f59c24262db5aaed365c7c1`

## Mobile browser gate

At 390×844, functional/security/visual metrics pass:

- `documentElement.scrollWidth == documentElement.clientWidth` (`375 == 375`); no document horizontal overflow;
- the tab strip has `clientWidth == scrollWidth == 375` and `scrollLeft == 0`;
- all three tab rectangles remain inside the document client width;
- active `Benchmark History`: `left=226.22`, `right=367`, fully legible;
- all three views are reachable;
- `extract: collected` and Linkup history render;
- the Overview provider table renders all 12 providers as cards with Provider, Readiness, Capabilities, Key, Auto, and Cooldown labels;
- the Benchmark table renders all three provider rows as cards with Provider, Score, Success rate, Median latency, and Errors labels;
- the Linkup card exposes `Median latency = 1.18 s` and `Errors = 0` in the initial card flow;
- every inspected table cell remained inside the viewport;
- all observed APIs returned HTTP 200;
- no error banner, page/console/request errors, token leakage, or path leakage;
- injected inline script remains blocked by CSP.

The same navigation and card assertions also pass at a stricter 320×800 stress viewport:

- document client/scroll width: `305 == 305`;
- tab strip client/scroll width: `305 == 305`;
- active `Benchmark History`: `left=181.2`, `right=297`;
- Median latency and Errors remain visible;
- all inspected cells remain inside the viewport.

Table/mobile-data verdict: **PASS**.

Overall visual verdict: **PASS** at 320 px and 390 px, with no desktop regression at 1440 px.

Screenshots:

- `/tmp/wsp-ws3-final-390-overview.png` — SHA-256 `6f457ab5c0cd4afa9c6516e478bfd120eb5891cc8af5cbf29cad3fec692b0334`
- `/tmp/wsp-ws3-final-390-benchmarks.png` — SHA-256 `566eb385867bc25591091c817083f4360d5081e125dee1cab468298f0526e19b`
- `/tmp/wsp-ws3-final-320-benchmarks.png` — SHA-256 `512009dbf40b0138f71c36dfbd63ea5e3d0a07a58c4881135b9292f4baa00ffe`

The fresh copied seven-file real-cache tree remained byte-identical after the complete final browser run.

## Static asset integrity

The supplied `index.html` and Revision-2 `app.js` remain byte-identical. The stylesheet digest is intentionally advanced by the authorized five-line mobile navigation correction:

- `index.html`: `c3e0e498ced835032c20ee2c7fed319e344fceb7fb5bc07c3449fc22e0c293d0`
- `styles.css`: `53603b96d0eb9e500452d03cfe368c6f49ff699d83e3521e7ddab4bdaeed7a93`
- `app.js`: `d3d631523c912d207a6fefdc61556b558544b9c433a91e1b19782907460c6757`

## Automated gates

- Full sterile pytest: **756 passed + 6 subtests passed**
- Focused frontend/server suite: **18 passed**
- Focused Extract benchmark tests: **10 passed**
- Adjacent Bench/Dispatch/Extract/Console/UI regression set: **121 passed** before the final additions
- Hermetic copied-real-cache acceptance: **1 passed**
- Ruff: PASS
- Python compileall: PASS
- Contract v3 schema reproducibility: PASS
- Node schema boundary: PASS
- `git diff --check`: PASS

## External closeout

The local release blocker is resolved and the exact committed HEAD is ready for independent external final `PASS | BLOCK` review. The earlier external Task-5 PASS remains valid for the static integration/security boundary; it is not reused as the final real-cache/mobile verdict.

## Required next action

Run the independent external final closeout against the committed HEAD and the evidence above. No additional local UI revision is required unless that review finds a concrete blocker.

No push, merge, release, active-plugin switch, config-priority change, or hosted exposure occurred.
