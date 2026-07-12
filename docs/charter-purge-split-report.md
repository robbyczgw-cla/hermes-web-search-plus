# Source-Only Charter Purge — Split Branch Report

**Branch:** `fix/source-only-charter-purge`  
**Base:** `c661497` (published v2.9.1 line plus PR #89)  
**Source commit:** live-correction slice extracted from `175da61` against its direct parent  
**Status:** local PASS candidate; not pushed, merged, released or installed

## Purpose

This branch isolates the live source-only correctness fix from the WSP v3 Evidence-Spine history. It does not contain M0/M1 contracts, RequestV3/ResponseV3 schemas, WS-1 observations, SQLite state, v3 cache, shadow routing, console work or RC artifacts.

A blind cherry-pick of `175da61` onto `c661497` was rejected because that commit sits on top of the M0/M1/M2/M3 history and changes 42 files. This branch applies only `175da61`'s direct commit delta for the live provider, registry, routing, formatter and request-gate surfaces, then adds a standalone v2 Charter-Gate suite.

## Behavior correction

- Provider registrations declare `source_results` or `source_text` semantics.
- Perplexity and Kilo-Perplexity remain diagnostic registry records but are no longer executable search capabilities because neither has a verified source-only endpoint.
- Perplexity/Kilo dispatch entries and default routing priority entries are removed.
- Tavily always sends `include_answer=false`.
- Linkup only sends `outputType=searchResults`.
- Exa is restricted to normal source-result mode; `deep` and `deep-reasoning` fail before network I/O.
- Provider adapters no longer manufacture or pass through `answer`/synthesis fields.
- The Hermes formatter cannot emit an `Answer:` block.
- Router enumeration excludes rejected/non-source modes.
- The central request gate rejects chat-message, answer-instruction and synthesis-shaped bodies before transport.

## Review surface

Production:

- `__init__.py`
- `provider_dispatch.py`
- `provider_registry.py`
- `providers.py`
- `request_gate_v3.py`
- `routing.py`
- `search.py`
- `scripts/golden_eval.py`
- generated `docs/PROVIDERS.md`

Tests:

- standalone `tests/test_charter_purge.py`
- focused provider/registry/routing/onboarding/golden-eval migrations

## Verification

### Standalone Charter gates

```text
12 passed
```

The gates cover descriptor semantics, banned labels, rejected providers, formatter output, plugin tool surface, Tavily/Linkup/Exa/Perplexity pre-network behavior, central outbound-body rejection, router eligibility and provider answer-field suppression.

### Focused migrated suite

```text
166 passed
```

### Full isolated-config suite

```text
487 passed in 4.12s
```

### Sterile gates

```text
ruff: passed
compileall: passed
git diff --check: passed
provider docs drift: current
routing docs drift: current
```

### Live Serper smoke

```json
{"provider":"serper","results":3,"banned_fields":false,"answer_key":false}
```

The installed Hermes plugin was not changed.

## Versioning and release honesty

This is not a silent patch: it removes two executable providers and bans previously accepted synthesis modes. Do **not** publish it as an ordinary `v2.9.2` bugfix without explicit release-policy approval.

Recommended release framing is a clearly documented behavior-changing Charter correction. The exact public version (`v3.0.0` prerelease versus another explicitly approved train) remains a release-owner decision. Release notes must name:

- Perplexity and Kilo-Perplexity execution removal;
- Exa deep/deep-reasoning removal;
- Linkup sourced-answer removal;
- Tavily answer suppression;
- the source-only engine invariant.

## Gates still required before publication

- Andy's independent Charter-Gate suite against this exact branch;
- fresh-clone and isolated-config smoke;
- migration/compatibility note for removed modes/providers;
- Robby's explicit GO before push, PR, merge, release or active-plugin switch.
