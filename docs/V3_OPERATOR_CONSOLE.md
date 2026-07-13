# WSP 3.0 Operator Console

The Operator Console is a local, read-only view of the v3 engine. It cannot run searches, call providers, mutate config, clear cache, change routing, or start benchmarks.

## Start

From the plugin directory:

```bash
python3 ui.py --port 8765
```

The process prints a one-time bootstrap URL similar to:

```text
http://127.0.0.1:8765/?token=...
```

Open that exact URL in a browser on the same machine. The bootstrap request exchanges the token for a strict local session cookie and redirects to `/`. Stop the server with Ctrl-C.

Use `--port 0` to let the operating system choose an unused loopback port.

## Security boundary

- The server binds only to literal `127.0.0.1`; wildcard, LAN, Tailnet, IPv6, and hostname binds are rejected.
- Only `GET` and `HEAD` are accepted.
- Host validation and token/session authentication are mandatory.
- Responses are `no-store` and include a restrictive CSP, frame denial, MIME sniffing protection, and no wildcard CORS.
- Static assets are loaded from bounded regular files without following symlinks.
- This server is not designed to sit behind a reverse proxy or be exposed remotely.

## Pages and data

The UI reads three privacy-filtered snapshots:

- `/api/v3/overview` — engine availability, provider capability/config booleans, bounds, cache totals, circuit totals, receipt summary, and benchmark summary;
- `/api/v3/receipts?limit=N` — newest sanitized execution receipts, with `N` clamped to 1–100;
- `/api/v3/benchmark-history?limit=N` — completed sanitized benchmark summaries.

The Console deliberately excludes queries, URLs, titles, snippets, extracted content, headers, credentials, endpoint URLs, and filesystem paths. Missing data is reported as unavailable or `not_collected`; the backend does not fabricate successful runs.

## Troubleshooting

- **`state_available=false`** — run `python3 search.py state-migrate` and check that the engine state exists under the active cache root.
- **No receipts** — execute normal v3 search or extraction calls first. Journal writes are best-effort and privacy validation fails closed.
- **No extraction benchmark history** — run an extract benchmark without `--no-history`.
- **401 after opening `/` directly** — use the fresh bootstrap URL printed at startup.
- **Port already in use** — choose another port or use `--port 0`.

The frozen security and DTO boundary is documented in [Operator Console Contract](v3-ws3-operator-console-contract.md).
