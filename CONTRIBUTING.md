# Contributing to Web Search Plus

Thanks for helping improve Web Search Plus. This project accepts focused bug fixes, documentation improvements, provider integrations, tests, and carefully justified reliability work.

Web Search Plus is a **source-only evidence engine**. It searches for and extracts inspectable sources; it does not expose provider modes that return synthesized answers, unsupported claims, or unverifiable citations. Contributions must preserve that boundary.

## Before you start

- Search existing issues and pull requests before opening another one.
- Keep one concern per pull request. Small, reviewable changes beat heroic bundles.
- Open an issue before large architecture changes, public-contract changes, or changes to default routing policy.
- Never include API keys, tokens, private URLs, query logs, local configuration, or machine-specific paths in code, fixtures, screenshots, commits, or PR text.

By contributing, you agree that your contribution is licensed under the repository's [MIT License](LICENSE).

## Development setup

CI supports Python 3.10, 3.11, and 3.12 and uses Node.js 22 for the JSON Schema boundary test. The commands below assume a POSIX shell; in Windows PowerShell, activate the environment with `.venv\Scripts\Activate.ps1` instead.

```bash
git clone https://github.com/robbyczgw-cla/hermes-web-search-plus.git
cd hermes-web-search-plus
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -r requirements-dev.txt
npm ci --ignore-scripts
```

Create a branch from the current `main` and add a regression test before changing behavior whenever practical. Tests must not depend on a contributor's Hermes installation, production configuration, cache, or paid-provider quota.

## Repository map

- `search.py`, `extract.py`, and `__init__.py` expose the public plugin paths.
- `runtime_v3.py`, `orchestrator_v3.py`, and `contract_v3.py` implement the native v3 evidence flow and wire contract.
- `provider_registry.py`, `provider_dispatch.py`, and `provider_adapter_protocol.py` define built-in provider metadata and adapter conformance.
- `wsp_sdk/` and `providers.d/` are the stable, additive provider-extension surface.
- `tests/` contains unit, contract, registry, routing, cache, privacy, and schema-boundary coverage.
- `scripts/` owns generated provider docs, routing docs, and v3 schemas.

Read [Architecture](docs/ARCHITECTURE.md) for system boundaries and the [Provider SDK guide](docs/PROVIDER_SDK.md) before changing those surfaces.

## Adding a provider

Prefer the zero-core-edit Provider SDK path:

```bash
python setup.py new-provider acme-search
```

This creates a self-contained module under `providers.d/` and a matching test under `tests/providers_d/`. Provider modules should import the stable `wsp_sdk` surface rather than internal registry, routing, search, or extraction modules.

A provider contribution must:

- return source URLs and source text through `source_result()`, `search_result()`, or `extract_result()`;
- never place a synthesized answer in the result envelope;
- use the scaffolded adapter signatures and typed SDK errors;
- use a dedicated environment-variable name for credentials without committing a real key;
- set `keyless=True` only for genuinely unauthenticated upstream access; keyless use still requires explicit operator opt-in;
- remain explicit-only by default. Promotion into automatic routing is a separate policy change that needs evidence and discussion;
- report filter support truthfully. If freshness, news search, locale, or another filter is not applied, metadata must not claim that it was;
- preserve existing URL-safety, SSRF, timeout, response-size, privacy, and source-envelope checks instead of bypassing them;
- mock upstream HTTP in automated tests. Live calls with your own key are optional and their responses must not be committed;
- update generated provider documentation and add a user-visible changelog entry.

Do not edit `plugin.yaml`, core provider lists, or routing defaults merely to make an SDK provider discoverable; discovery is automatic. See [Provider SDK](docs/PROVIDER_SDK.md) for the normative extension contract.

## Tests and required gates

Run the same checks as GitHub Actions before opening a pull request:

```bash
ruff check --config ruff.toml .
python -m pytest tests/ -q
python scripts/gen_provider_docs.py --check
python scripts/gen_contract_v3_schemas.py --check
node tests/schema_boundary_v3.mjs
python -m compileall -q .
git diff --check
git status --short
```

Generated files must not be edited by hand. When a source-of-truth change causes drift, regenerate the affected artifact and commit both source and output:

```bash
python scripts/gen_provider_docs.py
python scripts/gen_contract_v3_schemas.py
python scripts/gen_routing_docs.py
```

Every behavior change should include a test that fails without the change and passes with it. Prefer deterministic fixtures and mocked HTTP over brittle live tests. Do not hard-code the repository's total test count in documentation or PR claims; it changes frequently.

## Documentation and changelog

- Update public documentation whenever behavior, setup, configuration, provider capability, or compatibility changes.
- Add user-visible changes under `## [Unreleased]` in `CHANGELOG.md` using the existing category style.
- Preserve upstream authorship, project links, and license attribution when adapting ideas or integrations from another project.
- Do not manually edit generated `docs/PROVIDERS.md`, `docs/ROUTING.md`, or files under `schemas/v3/`.

## Pull requests

A good pull request contains:

- a concise problem statement and the intended behavior;
- the smallest coherent implementation;
- regression tests and the exact commands run;
- documentation and `[Unreleased]` updates where relevant;
- notes about compatibility, security, privacy, routing, or quota impact;
- no unrelated formatting, release, or refactoring churn.

CI must pass on every supported Python version. A maintainer may request a focused live-provider smoke for provider changes, but contributors are not required to spend quota or disclose credentials.

## Security and privacy reports

Do not include exploit details, credentials, private endpoints, or sensitive user data in a public issue. Open a minimal issue stating that you need a private reporting channel, without reproduction details, and wait for maintainer guidance.

For ordinary bugs, include a minimal reproduction, expected and actual behavior, relevant non-secret configuration shape, and sanitized logs.

## Maintainer-only work

Do not bump versions, run `scripts/prepare_release.py`, create tags or releases, publish packages, or rewrite release history in a contribution PR. Do not change automatic-provider defaults or promote a provider based only on marketing claims or a single live result. Maintainers handle release assembly and final routing-policy decisions.
