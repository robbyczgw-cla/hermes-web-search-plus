# Provider SDK (WSP 3.x)

`wsp_sdk` is the stable public surface for self-contained provider modules. It
is additive-only throughout 3.x: providers should import it, not internal
registry, dispatch, search, or extraction modules.

## Add a provider in three steps

1. Run `python setup.py new-provider acme-search`. This creates
   `providers.d/acme-search.py` and `tests/providers_d/test_acme_search.py`.
2. Implement the generated `execute_search` and/or `execute_extract` skeleton.
   Return `search_result(...)` or `extract_result(...)` and source items from
   `source_result(...)`. Results must be source-only; synthesized answers are
   rejected by the normal provider protocol gate.
3. Set the SDK fields in `PROVIDER = register_provider(ProviderSpec(...))`,
   add tests, then run the project suite. The module is discovered on startup;
   no edit to a core registry, priority list, config default, enum, README
   table, or plugin manifest is required.

## Provider declaration

Every `providers.d/*.py` file declares a module-level `PROVIDER`. Use an
`id` with lowercase letters, digits, and hyphens, `kind="search"`,
`"extract"`, or `"both"`, and a non-empty environment-variable name, signup
URL, display metadata, and config section. A search provider supplies:

```python
def execute_search(search_module, prov, args, key, config, routing_info):
    return search_result(prov, args.query, [source_result("https://source.example")])
```

Extraction providers use the formal ten-argument `execute_extract` skeleton
created by the scaffold. The signatures are checked at discovery time, before
the provider is exposed to any user-facing command.

Set `keyless=True` only when public unauthenticated access is genuinely
supported. Keyless access still requires explicit operator opt-in through
`<config_section>.allow_public` or `<CONFIG_SECTION>_ALLOW_PUBLIC=1`.

## Safety and discovery behavior

New providers are explicit-only by default. They join `provider="auto"` only
when their declaration sets `auto_allowed_by_default=True` and the existing
`auto_allow` configuration admits them. This prevents a newly dropped-in
module from receiving production traffic unexpectedly.

Duplicate IDs fail closed with `DuplicateProviderError`; there is no
import-order winner. A syntax error, missing `PROVIDER`, or invalid declaration
becomes a `ProviderStartupDiagnostic`, leaves the plugin running, and excludes
that module. Startup diagnostics intentionally contain stable module names and
codes, not file paths or upstream error text.

`ProviderConfigError`, `ProviderRequestError`, `ProviderContractFailure`,
`ProviderRegistrationError`, `ProviderDiscoveryError`,
`DuplicateProviderError`, and `RequestDeadlineExceeded` are the typed SDK
errors providers may use.
`wsp_sdk.conformance` runs the shared metadata,
formal-dispatch, source-envelope, keyless, and missing-key checks over every
built-in and discovered spec without making network requests.

A provider with its own transport (not `wsp_core.http_client`) calls
`dispatch_timeout(timeout)` right before each request: it returns the socket
timeout reduced to the engine's per-attempt cap and the time left until the
request deadline, and raises `RequestDeadlineExceeded` (a timeout-class
`ProviderRequestError`) once that deadline has passed. A provider that makes
several requests per call checks `deadline_passed()` before each one and
reports the skipped items instead of starting them. `remaining_seconds()`
returns the time left, or `None` without a deadline.

The committed `providers.d/example_fixture.py` is a no-network, non-production
proof of the extension path. It is listed by status/doctor/bench and accepted
for explicit dispatch only when its keyless public access is enabled; it is
never in the default auto pool.

The bundled [Parallel Search MCP provider](PARALLEL_MCP.md) demonstrates an
explicit-only keyless HTTP provider with bounded MCP requests and source-only
projection.
