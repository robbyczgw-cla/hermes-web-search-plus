from __future__ import annotations

import importlib.util
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_VERSION = "3.0.0"


def _load_plugin_module():
    spec = importlib.util.spec_from_file_location("wsp_release_metadata_under_test", ROOT / "__init__.py")
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_release_version_surfaces_are_in_sync():
    # The one deliberately hardcoded version gate. Bump every surface at once
    # with: python3 scripts/prepare_release.py <new-version> --write
    plugin = _load_plugin_module()
    plugin_yaml = (ROOT / "plugin.yaml").read_text()
    init_py = (ROOT / "__init__.py").read_text()
    search_py = (ROOT / "search.py").read_text()
    http_client_py = (ROOT / "http_client.py").read_text()
    operator_console_py = (ROOT / "operator_console_v3.py").read_text()
    ui_py = (ROOT / "ui.py").read_text()
    changelog = (ROOT / "CHANGELOG.md").read_text()

    assert plugin.__version__ == EXPECTED_VERSION
    assert f'version: "{EXPECTED_VERSION}"' in plugin_yaml
    assert f"Hermes Plugin v{EXPECTED_VERSION}" in init_py
    assert f"Version: {EXPECTED_VERSION}" in search_py
    assert f'DEFAULT_USER_AGENT = "ClawdBot-WebSearchPlus/{EXPECTED_VERSION}"' in http_client_py
    assert f'plugin_version: str = "{EXPECTED_VERSION}"' in operator_console_py
    assert f'plugin_version: str = "{EXPECTED_VERSION}"' in ui_py
    assert re.search(rf"^## \[v{re.escape(EXPECTED_VERSION)}\] — \d{{4}}-\d{{2}}-\d{{2}}$", changelog, re.M)


def test_runtime_requirements_stay_stdlib_only():
    assert (ROOT / "requirements.txt").read_text().strip() == ""
