from __future__ import annotations

import importlib.util
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_VERSION = "3.4.1"


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


def test_ci_ruff_policy_is_repo_local_and_pinned():
    policy = (ROOT / "ruff.toml").read_text()
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    requirements = (ROOT / "requirements-dev.txt").read_text()

    assert 'select = ["E4", "E7", "E9", "F"]' in policy
    assert "ruff check --config ruff.toml ." in workflow
    assert "ruff==0.15.12" in requirements


def test_current_release_surfaces_and_attribution():
    readme = (ROOT / "README.md").read_text()
    changelog = (ROOT / "CHANGELOG.md").read_text()
    release_notes = (ROOT / "docs/RELEASE_NOTES_V341.md").read_text()
    user_guide = (ROOT / "docs/USER_GUIDE.md").read_text()
    combined = "\n".join((readme, changelog, release_notes, user_guide))

    assert "Current release: **v3.4.1**" in readme
    assert "docs/RELEASE_NOTES_V341.md" in readme
    assert "Exa" in release_notes
    assert "TinyFish" in release_notes
    assert "explicit-only" in release_notes
    assert "training" in release_notes
    assert "https://github.com/dondai1234/master-fetch" in combined
    assert "Bishesh Bhandari" in combined
    assert "MIT-licensed" in combined or "MIT project" in combined
    assert "https://github.com/dondai1234/hound" not in combined


def test_readme_intro_uses_plain_product_language():
    readme = (ROOT / "README.md").read_text()
    intro = readme.split("## Quick Start", 1)[0].lower()

    for implementation_term in (
        "heading-aware",
        "provenance-safe",
        "quality-quorum",
        "observation id",
        "fetch_priority",
    ):
        assert implementation_term not in intro

    assert "better web search and clean page reading" in intro
    assert "## why use it" in intro
