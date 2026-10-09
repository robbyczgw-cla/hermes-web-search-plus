"""Moving the engine into wsp_core/ must not move any user file.

Cache, provider health/stats, v3 state, config.json, the plugin .env files and
providers.d are located relative to the plugin directory. Their defaults used
to hang off each module's own location; after the move they hang off the
plugin (host) directory and must resolve to exactly the same places.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROBE = r'''
import json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from wsp_core import cache, config, env_loader, provider_registry, state_migration_v3
print(json.dumps({
    "cache_dir": str(cache.CACHE_DIR),
    "config_default": str(config.HOST_DIR.parent / "config.json"),
    "env_candidates": [str(p) for p in env_loader.candidate_env_paths(config.HOST_DIR / "__init__.py")],
    "providers_dir": str(provider_registry.PROVIDERS_DIRECTORY),
    "migration_cache_root": str(state_migration_v3._default_cache_root()),
}))
'''


def _defaults() -> dict:
    proc = subprocess.run(
        [sys.executable, "-I", "-c", PROBE, str(ROOT)],
        capture_output=True, text=True, timeout=60,
        env={"HERMES_HOME": "/nonexistent-hermes-home", "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_default_paths_did_not_move_with_the_engine():
    defaults = _defaults()

    assert Path(defaults["cache_dir"]) == ROOT.parent / ".cache"
    assert Path(defaults["config_default"]) == ROOT.parent / "config.json"
    assert Path(defaults["providers_dir"]) == ROOT / "providers.d"
    assert Path(defaults["migration_cache_root"]) == ROOT.parent / ".cache"
    candidates = [Path(path) for path in defaults["env_candidates"]]
    assert candidates[:2] == [ROOT / ".env", ROOT.parent / ".env"]
