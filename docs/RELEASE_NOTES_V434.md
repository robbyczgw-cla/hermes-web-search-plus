# Web Search Plus 4.3.4

Fixes for the first run without provider keys. No routing, provider or config format changes.

## Fixed

- **`setup.py status` no longer says `ready` with zero providers.** `profile.ready` now also requires a configured search provider. The profile policy check on its own is reported as `policy_ready`.
- **Status sees keys stored in the plugin config.** Keys and keyless opt-ins in the plugin config are counted, not only `.env`.
- **Clear error when nothing is configured.** Search and extract with no configured provider return `error_type: provider_setup_required`, the command for the matching preset (`setup.py setup --preset starter`, `extract` or `self-hosted`) and that preset's env vars.
- **Targeted error for an explicit provider.** `--provider serpbase` without a Serpbase key returns `requested_provider_not_configured` and `setup.py setup serpbase`, even when other providers are configured. Real upstream errors from configured providers are left unchanged.
- **Missing-key hints point to `.env`.** They suggest `setup.py setup <provider>` or the profile `.env`, no longer an inline key in `config.json`.
- **`search.py --extract-urls` exit code.** It exits 1 and writes to stderr when no URL returned content.

- **DonSeTch stderr on close.** Closing a session now lets the stderr reader finish before the pipe is closed. Previously the sanitized stderr excerpt could be lost and the reader thread could raise `ValueError` under load.

## Changed

- The status dashboard shows `--preset starter` and the reload hint only when they apply, and its commands use `python3`.
- Error payloads may carry the new `error_type` values above. Scripts that only checked `error` keep working.
