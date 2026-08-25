from __future__ import annotations

import importlib.util
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_VERSION = "4.0.3"


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
    release_notes = (ROOT / "docs/RELEASE_NOTES_V400.md").read_text()
    user_guide = (ROOT / "docs/USER_GUIDE.md").read_text()
    env_template = (ROOT / ".env.template").read_text()
    current_surfaces = "\n".join((readme, release_notes, user_guide, env_template))

    assert "Current release: **v4.0.3**" in readme
    assert "docs/RELEASE_NOTES_V400.md" in readme
    assert "DonSeTch 2.1.0" in release_notes
    assert "explicit-only" in release_notes
    assert "DONSETCH_BIN" in current_surfaces
    assert "HOUND_MCP_URL" not in env_template
    assert "https://github.com/dondai44423/donsetch" in current_surfaces
    assert "AGPL-3.0-only" in current_surfaces
    assert "https://github.com/dondai1234/master-fetch" not in current_surfaces
    assert "docs/HOUND.md" not in current_surfaces


def test_current_filter_help_mentions_all_new_native_support():
    init_py = (ROOT / "__init__.py").read_text().lower()
    search_py = (ROOT / "search.py").read_text().lower()
    user_guide = (ROOT / "docs/USER_GUIDE.md").read_text().lower()

    assert "google.serper.dev/news) and tinyfish" in init_py
    assert "currently serper and tinyfish" in search_py
    assert "searxng, exa, and tinyfish" in search_py
    assert "searxng, exa, and tinyfish" in user_guide
    assert "tinyfish serves it through its native news-domain mode" in user_guide


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
