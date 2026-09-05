# Native Hermes backend

The `wsp` backend lets Hermes' `web_search` and `web_extract` tools call the existing WSP engine in-process. It addresses the integration requested in [#125](https://github.com/robbyczgw-cla/hermes-web-search-plus/issues/125) without the separate CLI wrapper proposed there.

This is an opt-in interface, not a change in search quality or provider policy. Installing WSP does not select it. `web_search_plus` and `web_extract_plus` retain their current schemas and defaults.

## Select the backend

Use a Hermes version that supports `PluginContext.register_web_search_provider` and the `WebSearchProvider` API. Load and enable the existing `web-search-plus` plugin through Hermes. If your Hermes version asks for a provider capability grant, review and grant it through Hermes; this plugin does not bypass that decision.

In the active profile's Hermes `config.yaml`, explicitly select either capability:

```yaml
web:
  search_backend: wsp
  extract_backend: wsp
```

For mixed routing, keep your extraction provider:

```yaml
web:
  search_backend: wsp
  extract_backend: firecrawl
```

The shared `web.backend: wsp` setting is also supported. Per-capability settings take precedence. WSP's own routing configuration still controls which underlying provider its `auto` route uses. Explicit-only providers, including DonSeTch, are not automatically opted into the routing pool.

Native tools require the Hermes `web` toolset enabled. If you previously configured Plus-only Fastpath, remove **only** `web` from `agent.disabled_toolsets`, retaining any other disabled toolsets. Inspect the configuration without changing it:

```bash
python ~/.hermes/plugins/web-search-plus/setup.py fastpath --json
```

For another profile, use that profile's plugin path and `HERMES_HOME`, or pass `--config-path` explicitly. The doctor inspects configuration; it does not prove a loaded backend or working credentials. Apply runtime changes in a fresh Hermes process when no work is active.

## Behavior and limits

- No CLI fallback: an unavailable in-process engine produces an error. `WSP_FORCE_SUBPROCESS` is incompatible with the native path; it continues to work for the existing Plus tools.
- Native search uses WSP's 20-result cap. Hermes may bucket a smaller request before dispatch; the adapter reports the effective cap and Hermes slices the final rows.
- Native tool schemas do not expose the full Plus feature set. Use Plus tools for per-call provider choice, research mode, locale/domain filters, spans, or detailed quality reports.
- Extraction preserves requested URL association and reports missing or failed items individually. The native Hermes wrapper also applies its own content-size limits.
- The bridge uses WSP's existing request timeout. A timeout ends the caller's wait; Python cannot forcibly stop an already-running provider thread. It is not a guarantee that upstream network work was cancelled.
- Hermes' outer search cache keys on the backend name `wsp`, query and count, not WSP's inner provider settings. After changing inner routing or credentials, use a fresh Hermes process rather than expecting an identical cached query to prove the new settings. Do not share one native runtime across different profiles or credential contexts.

## Rollback

Restore your previous `web.search_backend` and `web.extract_backend` settings (and `web.backend`, if set), then apply them in a fresh process. For Plus-only mode, disable the `web` toolset as before. Disabling or unloading WSP removes its native provider registration through Hermes' registration handle; it does not rewrite backend pins in your configuration.

No separate `web/wsp` wrapper plugin is required. Remove or disable a previously installed wrapper before selecting this backend so two plugins do not compete for the `wsp` name.
