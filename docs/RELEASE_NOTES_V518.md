# Web Search Plus 5.1.8

Setup tells the truth. Whether a provider can run is now decided in one place, and every surface — `status`, setup, the tools' availability, the session hint, the engine and the native backend — shows that one answer, with the reason and the command that fixes it. Found by an analysis of the plugin's implicit state machines; checked with the real Hermes v0.22.5 CLI in a throwaway home. No configuration changes are needed.

## Fixed

- **Keyless setup under Hermes.** `hermes web-search-plus setup --keyless-public` used to write a flag into config.json that Hermes ignores, print "✓ Enabled", list the tools, and then every search failed with "serper: Missing API key"; running setup again said "Nothing changed". Now setup prints `hermes config set KEENABLE_ALLOW_PUBLIC 1`, and if the flag is already in config.json, `status`, the session hint and tool errors say so first, with that command.
- **Errors name the real problem.** A template placeholder key, a private SearXNG URL, a disabled provider or an ignored config.json setting is named with its fix, instead of "setup --preset starter" and a Serper line.
- **No false "configured".** Placeholders and blocked SearXNG URLs no longer count as configured. The tool stays listed in those cases so a call can tell the model what to fix.
- **DonSeTch binary from config.json or Hermes Desktop** now works; a path that does not exist is reported as such.
- **Native backend** passes setup messages on instead of "WSP extraction failed for this URL".

## Also in 5.1.8 (from the same analysis)

- **Research status is stable.** A research answer whose third member was still running when the first two agreed was marked `degraded` with "limited by budget" — in 20 of 20 runs; now 0 of 20. A real time-budget overrun still reports `degraded`.
- **Research says when it could not read its sources**, with the reason, instead of silently returning no page text.
- **`fallback_reason` agrees** between the v3 receipt and the legacy routing (20/20 mismatches → 0/20).
- **DonSeTch errors have real classes:** a hung binary is a timeout (retried once, then the circuit breaker stops further waits) instead of "Provider execution failed"; a binary that does not start names the fix; `freshness` and `format: html` run and report that they were not applied.
- **`setup --no-jev`** switches an enabled Jev off.
- **Engine import is retried** after 30 seconds instead of staying broken until Hermes restarts; the tools are hidden while it cannot load.
- **Healthy providers no longer show as "open"** in the operator console (non-blocking errors no longer write circuit rows).
- **Daily budget reservations left by a crashed process** are reclaimed after 600 s and counted as used (never refunded): 10 simulated crashes left 10 stuck reservations, now 0.

## For scripts and agents

`hermes web-search-plus status --json` keeps every key and adds, per provider, `state` (`ready`, `explicit_only`, `disabled`, `not_configured`, `ignored_source`, `invalid_value`, `scope_error`), `reason_code`, `reason`, `fix` and `source`. v3 setup failures are `error_class: config`, `retryable: false`, with `details.readiness`.

## Checked before release

- Real Hermes CLI, throwaway home, keyless setup: writes nothing, prints the profile command; after `hermes config set KEENABLE_ALLOW_PUBLIC 1` status reports Keenable `ready` (source `hermes_profile`). The same flow on 5.1.7 reports "configured" and loops.
- Real Hermes registry dispatch with only a Brave key: search returns Brave results; extract says "No configured extract provider … setup --preset extract".
- `tests/test_readiness_agreement.py` checks status, check_fn, engine and tool text against each other under a Hermes-style resolver and a standalone host; 11 of its tests fail on 5.1.7.

Full list: [CHANGELOG](../CHANGELOG.md).
