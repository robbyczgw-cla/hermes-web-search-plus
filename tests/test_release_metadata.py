from __future__ import annotations

import importlib.util
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_VERSION = "2.2.1"


def _load_plugin_module():
    spec = importlib.util.spec_from_file_location("wsp_release_metadata_under_test", ROOT / "__init__.py")
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_release_version_surfaces_are_in_sync():
    plugin = _load_plugin_module()
    plugin_yaml = (ROOT / "plugin.yaml").read_text()
    search_py = (ROOT / "search.py").read_text()
    changelog = (ROOT / "CHANGELOG.md").read_text()

    assert plugin.__version__ == EXPECTED_VERSION
    assert f'version: "{EXPECTED_VERSION}"' in plugin_yaml
    assert f"Version: {EXPECTED_VERSION}" in search_py
    assert re.search(rf"^## \[v{re.escape(EXPECTED_VERSION)}\] — \d{{4}}-\d{{2}}-\d{{2}}$", changelog, re.M)


def test_runtime_requirements_stay_stdlib_only():
    assert (ROOT / "requirements.txt").read_text().strip() == ""
