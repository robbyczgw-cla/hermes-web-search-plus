# WSP v3 WS-3 Task-1 Gate — DTO, Receipt and Security Contract

**Status:** ROBBY REVIEW REQUIRED · hard stop before Task 2  
**Branch:** `feat/v3-evidence-spine`  
**Base HEAD:** `7fa603b`  
**Date:** 2026-07-12  
**Production/UI code added:** none

## Verdict requested

Approve or block the frozen Task-1 contract and fixtures. Task 2 — Routing-v2 Receipt completion and journal implementation — MUST NOT begin without Robby's explicit acceptance of this gate.

## Frozen artifacts

- `docs/v3-ws3-operator-console-contract.md`
  - SHA-256 `8d65e13a8b6c5724d9ddb352cbc1fd80076c7ac9f4a816a5281e16bf1f88a411`
- `tests/fixtures/v3/ws3/overview.json`
  - SHA-256 `97780a0e6ee420feeff696e27ed75c492da926feb66b0ee9658d09ac970c8d52`
- `tests/fixtures/v3/ws3/receipts.json`
  - SHA-256 `f9959ab8b5f8ff89399f288fe75b0a911a056b9f0bdfd6be8d5ebaef30cbfe03`
- `tests/fixtures/v3/ws3/benchmark-history.json`
  - SHA-256 `9e9daf058f04c2125a9ca28295c5d617b8e5f8c38dc9e791bc71c2544b9be08b`
- `tests/test_ws3_contract.py`
  - SHA-256 `5351965525531ac4e249190d11fce2c525351bac14a8d65ff929a7ccdc671018`

## Robby's three acceptance additions

### 1. One recursive forbidden-field choke point

The contract requires exactly one production function:

```python
operator_privacy_v3.assert_operator_payload_safe(payload)
```

It must recursively guard:

- overview endpoint output;
- receipts endpoint output;
- benchmark-history endpoint output;
- every receipt immediately before journal persistence.

The RED suite requires all three endpoint serializers and the journal encoder to invoke that same function object. Per-endpoint copies are contract violations.

Forbidden keys cover query/URL/source text/fulltext, credentials/secrets/auth/headers, credential identity, endpoint URL and filesystem path surfaces. Absolute paths, credential-bearing URIs and authorization strings are forbidden values.

### 2. Loopback-only binding

The contract requires `127.0.0.1` only. The RED matrix explicitly rejects:

- `0.0.0.0`;
- `::`;
- LAN address `192.168.1.20`;
- Tailnet/CGNAT address `100.100.100.100`.

The product host is not configurable to non-loopback. A temporary Tailnet preview may only use an external local proxy that preserves product Host/token enforcement and is never committed or shipped.

### 3. Cache origin/current separation

The cache-hit fixture requires:

- zero current provider attempts;
- empty current candidate order and decisions;
- null current selected provider;
- `execution_scope="current"`;
- separate `cache_origin` with origin execution/policy/candidates/selection;
- no origin attempt IDs in the journal-facing DTO.

The origin subreceipt is historical evidence, never a claim that a provider ran for the current cache-served execution.

## Routing-v2 Receipt completion frozen for WS-3A

Additive fields:

- `authority="classic"`;
- `execution_scope="current"`;
- typed `candidate_decisions[]`;
- nullable `cache_origin`;
- nullable `shadow_observation` with `affected_execution=false`.

Closed candidate decision reasons:

- `classic_selected`
- `fallback_selected`
- `attempt_failed`
- `blocked_auth`
- `blocked_quota`
- `circuit_open`
- `budget_denied`
- `provider_unavailable`
- `not_attempted_after_success`
- `cache_origin_selected`

This completes receipt truth for direct success, fallback, skip, failure, cache hit and shadow observation. It does not redesign provider selection; classic Routing-v2 remains authoritative.

## Fixture coverage

- Overview: provider readiness, WS-2 defaults/hard bounds, owned cache stats, aggregate circuit state, receipt/benchmark summaries — no paths or secrets.
- Receipts: fresh cache hit with origin/current separation plus degraded extraction fallback with shadow observation and WS-2 truncation metadata.
- Benchmark history: typed `kind="search"`, extraction explicitly `not_collected`, never a fabricated zero-result run.

## RED evidence

Command:

```bash
python3 -m pytest tests/test_ws3_contract.py -q -p no:cacheprovider
```

Result:

```text
4 passed, 8 failed
```

The four passing tests prove fixture self-consistency now:

- all three endpoint fixtures recursively secret-free;
- reason codes within the frozen closed set;
- cache-hit origin/current separation;
- shadow observation cannot affect execution.

The eight intentional failures prove missing production boundaries rather than malformed tests:

- shared `operator_privacy_v3` choke point absent;
- shared endpoint/journal serializer wiring absent;
- typed `CandidateReasonCode` and receipt validator absent;
- operator snapshot builders absent;
- UI server absent for each of four forbidden bind targets.

No stub or production implementation was added to manufacture GREEN.

## Non-regression evidence

```text
35 passed
ruff: PASS
three JSON fixtures: valid
git diff --check: PASS
```

The 35 passing tests are the complete focused WS-2 bounded-context/retention/entrypoint set.

## Gate decision

- **PASS:** freeze this Task-1 checkpoint and authorize Task 2 implementation only.
- **BLOCK:** name the field, reason code, fixture, privacy rule or bind/cache invariant requiring correction.

Even after PASS: no Console UI code until Receipt completion/journal are green; no push, PR, merge, release, deploy, preview exposure or active-plugin switch without the corresponding separate GO.
