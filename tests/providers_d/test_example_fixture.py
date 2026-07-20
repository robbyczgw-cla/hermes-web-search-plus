"""SDK example-fixture discovery: opt-in only, never in the default surface."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import provider_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
PROBE = Path(__file__).with_name("_fixture_probe.py")


def test_fixture_provider_is_absent_without_the_explicit_opt_in() -> None:
    assert "example-fixture" not in provider_registry.PROVIDER_SPECS
    assert "example-fixture" not in provider_registry.SEARCH_PROVIDER_IDS
    assert "example-fixture" not in provider_registry.DEFAULT_AUTO_ALLOW


def test_fixture_provider_zero_core_edit_path_with_opt_in(tmp_path: Path) -> None:
    env = dict(os.environ)
    env[provider_registry.NON_PRODUCTION_DISCOVERY_ENV] = "1"
    env["PYTHONPATH"] = str(REPO_ROOT)
    isolated_cache = tmp_path / "cache"
    env["WSP_CACHE_DIR"] = str(isolated_cache)
    proc = subprocess.run(
        [sys.executable, str(PROBE)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "FIXTURE_PROBE_OK" in proc.stdout
    assert (isolated_cache / "operator" / "v3" / "receipts.jsonl").is_file()
