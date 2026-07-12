# Contract Amendment 001 — M1 public search parity

Status: **engine-owner candidate; policy-owner acceptance required at M1 handoff.**

## Reason

The co-signed M0 `SearchOptions` could not losslessly represent four inputs already exposed by the v2 Hermes tool. Omitting them would force the compatibility layer to use hidden runtime context, violating B6 because an identical `RequestV3` could then produce a different provider plan or output.

## Additions

`SearchOptions` gains four optional, strictly typed fields:

- `depth: normal | deep | deep-reasoning`
- `mode: normal | research`
- `quality_report: boolean`
- `research_time_budget: number` in the inclusive range 1–75 seconds

No existing field, default, provider behavior, response field, or golden response fixture changes. Unknown fields remain rejected.

## Compatibility boundary

The amendment covers the public Hermes `web_search_plus` surface. Provider-specific standalone CLI tuning flags remain adapter internals and are not promoted into the provider-agnostic v3 wire contract.

The byte-compatible v2 response is carried only in the internal `ExecutedV3` envelope. It is not serialized into `ResponseV3`, does not alter the response schema, and cannot become a second execution path.
