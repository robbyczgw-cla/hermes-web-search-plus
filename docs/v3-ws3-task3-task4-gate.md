# WS-3 Task 3+4 Gate — Snapshot Backend and Hardened Local API

**Status:** PASS
**Date:** 2026-07-12
**Branch:** `feat/v3-evidence-spine`
**Scope:** Task 3 read-only snapshots + Task 4 loopback-only HTTP API

## Delivered

- `operator_console_v3.py`
  - deterministic serializers for overview, receipts and benchmark history
  - every DTO crosses `operator_privacy_v3.assert_operator_payload_safe`
  - receipt snapshots read the atomic marker-owned journal without creating locks, pruning or rewriting
  - benchmark snapshots read only marker-owned bounded JSONL and report `not_collected` truthfully
  - overview derives readiness from the public provider registry/config and aggregates owned cache/state only
  - missing, malformed, foreign or symlinked storage fails closed
- `ui.py`
  - literal `127.0.0.1` binding only
  - startup Bearer token with constant-time comparison
  - strict loopback Host validation
  - exactly three JSON routes under `/api/v3/`
  - `GET` and `HEAD` only; arbitrary other methods return `405`
  - no CORS and no request logging
  - `no-store`, CSP, nosniff, frame, referrer and same-origin resource headers on success and errors
- Task-2 advisory resolved
  - WS-3 receipt fixtures now use production-shape-valid execution and attempt IDs
  - `_FROZEN_FIXTURE_IDS` was removed from production privacy code

## Read-only proof

A disposable real-backend localhost smoke exercised:

- `GET /api/v3/overview` → `200`
- `GET /api/v3/receipts?limit=5` → `200`
- `GET /api/v3/benchmark-history?limit=5` → `200`
- `HEAD /api/v3/overview` → `200`, empty body and truthful `Content-Length`
- missing token → `401`
- foreign Host → `421`
- files created under disposable cache root → `0`

The API never calls providers, starts benchmark traffic, mutates configuration, prunes caches or initializes SQLite.

## Gate evidence

- Full pytest suite: **740 passed + 6 subtests passed**
- Task 3+4 contract/security suite: **30 passed**
- Frozen Task-1 fixtures: exact DTO equality from real seeded owned stores
- Ruff: PASS
- Python compileall: PASS
- JSON Schema boundary: PASS
- Schema regeneration reproducibility: PASS
- Git whitespace check: PASS
- Operator-surface static scan: PASS

## Security invariants covered

- non-loopback bind refusal includes wildcard, IPv6, LAN and Tailnet/CGNAT addresses
- symlinked benchmark files, cache roots and state ancestors are rejected
- journal reads are directory-FD anchored and never invoke mutating retention logic
- unknown HTTP methods such as `PROPFIND` and arbitrary methods cannot fall through to stdlib `501` HTML
- Host and authorization failures happen before snapshot builders run
- configured secrets are supplied to the shared serializer boundary for negative leak detection

## Explicit non-goals of this checkpoint

- no HTML/CSS/JavaScript frontend — Task 5
- no provider calls or quota-spending benchmark run
- no real Extract benchmark collector/history writer — remains required for 3.0 after the Console
- no self-hosted/no-paid-key profile — explicitly sequenced to 3.1
- no push, PR, release or active-plugin switch

## Remaining WS-3 sequence

1. Task 5: static premium frontend using only these APIs.
2. Real Extract benchmark evidence required by the accepted 3.0 boundary.
3. Task 6: browser/disposable/real-cache gates and Andy/Opus external completion review.
