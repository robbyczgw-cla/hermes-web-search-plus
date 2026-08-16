# Web Search Plus 4.0.0

## Removed

- Removed the optional Hound provider and its `HOUND_MCP_URL` configuration.
  Delete old Hound settings before upgrading.

## Added

- Added DonSeTch 2.1.0 as a local stdio provider for source Search and
  Markdown Extract.
- Configure the separately installed executable with `DONSETCH_BIN`.
- DonSeTch remains explicit-only by default and is not bundled with Web Search
  Plus. DonSeTch is licensed under AGPL-3.0-only; review that license for your
  deployment.

## Adapter boundary

The WSP adapter starts DonSeTch as a local stdio MCP process and normalizes
its `initialize`, `web_search`, `web_fetch`, and structured-error responses
into WSP's existing source-only envelopes. It does not embed DonSeTch code or
redistribute the DonSeTch binary.

The adapter was tested against DonSeTch 2.1.0 for stdio initialization, Search,
Fetch, and structured error handling. The standalone MCP wrapper gives explicit
DonSeTch calls a 195-second outer subprocess budget, covering the adapter's
180-second inner timeout. Browser-based retrieval remains
runtime-dependent; Web Search Plus makes no guarantee about which sites a
particular host can retrieve.

## Upgrade

1. Remove `HOUND_MCP_URL` from the Hermes environment/configuration.
2. Install DonSeTch 2.1.0 separately.
3. Set `DONSETCH_BIN` to the absolute DonSeTch executable path.
4. Change explicit `provider="hound"` calls to `provider="donsetch"`.
5. Keep DonSeTch explicit-only until you have verified it in your own
   environment; automatic routing can be enabled deliberately afterward.
