# AGENTS.md

## Cursor Cloud specific instructions

This repo is a single product: the **web-search-plus** Hermes Agent plugin (Python 3.8+,
**stdlib-only runtime**). There is no backend service, database, Docker stack, or build step —
everything is plain Python modules loaded directly.

### Dev dependencies

Only `pytest` and `ruff` are needed for development (installed by the update script). They land in
`~/.local/bin`, which is **not on `PATH`**. Invoke them as modules to avoid PATH issues:

- Lint: `python3 -m ruff check .`
- Tests: `python3 -m pytest tests/ -q`
- Compile check (mirrors CI): `python3 -m py_compile search.py __init__.py daemon_tasks.py http_client.py cache.py config.py env_loader.py provider_health.py provider_stats.py quality.py research.py routing.py providers.py extract.py scripts/golden_eval.py`

The CI definition lives in `.github/workflows/ci.yml` (ruff + pytest + py_compile).

### Running the app (CLI)

There is no long-running server. The product is exercised via two CLIs:

- `python3 setup.py status|list|setup` — provider onboarding / inspection (see `setup.py`, `README.md`).
- `python3 search.py --query "..." [--provider ...] [--quality-report]` — web search.
- `python3 search.py doctor` — environment/provider diagnostics.

### Live search without any API keys

Providers normally need API keys (`SERPER_API_KEY`, `TAVILY_API_KEY`, `LINKUP_API_KEY`, etc.; see
`.env.template`). For a keyless end-to-end smoke test, use the Keenable public tier:

```bash
export KEENABLE_ALLOW_PUBLIC=1
export WSP_CACHE_DIR=/tmp/wsp_cache   # avoids writing to ~/.hermes
python3 search.py --query "Hermes Agent latest release" --provider keenable --quality-report
```

Note: the keyless tier sends queries/URLs to an unauthenticated shared service with no SLA and is
rate-limited — fine for smoke tests, not for load. Set `WSP_CACHE_DIR` (or `WEB_SEARCH_PLUS_CONFIG`)
when running from a flat checkout so state doesn't default to `~/.hermes/plugins`.
