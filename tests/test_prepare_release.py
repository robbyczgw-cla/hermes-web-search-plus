"""Tests for scripts/prepare_release.py (single-step version bump tool)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "prepare_release.py"
spec = importlib.util.spec_from_file_location("wsp_prepare_release_under_test", SCRIPT_PATH)
prepare_release = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(prepare_release)


OLD = "1.2.3"


def _make_fake_repo(root: Path, version: str = OLD) -> None:
    (root / "tests").mkdir(parents=True)
    (root / "plugin.yaml").write_text(f'name: web-search-plus\nversion: "{version}"\n', encoding="utf-8")
    (root / "__init__.py").write_text(
        f'"""web-search-plus — Hermes Plugin v{version}"""\n__version__ = "{version}"\n',
        encoding="utf-8",
    )
    (root / "search.py").write_text(f'"""\nVersion: {version}\n"""\n', encoding="utf-8")
    (root / "http_client.py").write_text(
        f'DEFAULT_USER_AGENT = "ClawdBot-WebSearchPlus/{version}"\n', encoding="utf-8"
    )
    (root / "tests" / "test_release_metadata.py").write_text(
        f'EXPECTED_VERSION = "{version}"\n', encoding="utf-8"
    )
    (root / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n### Added\n- something new\n\n"
        f"## [v{version}] — 2026-01-01\n\n- old entry\n",
        encoding="utf-8",
    )


def _run(root: Path, *argv: str) -> int:
    return prepare_release.main(list(argv) + ["--root", str(root)])


def test_dry_run_changes_nothing(tmp_path):
    _make_fake_repo(tmp_path)
    before = {p: p.read_text(encoding="utf-8") for p in tmp_path.rglob("*") if p.is_file()}

    assert _run(tmp_path, "2.0.0", "--date", "2026-07-04") == 0

    after = {p: p.read_text(encoding="utf-8") for p in tmp_path.rglob("*") if p.is_file()}
    assert before == after


def test_write_updates_every_surface(tmp_path):
    _make_fake_repo(tmp_path)

    assert _run(tmp_path, "2.0.0", "--date", "2026-07-04", "--write") == 0

    assert 'version: "2.0.0"' in (tmp_path / "plugin.yaml").read_text(encoding="utf-8")
    init_py = (tmp_path / "__init__.py").read_text(encoding="utf-8")
    assert '__version__ = "2.0.0"' in init_py
    assert "Hermes Plugin v2.0.0" in init_py
    assert "Version: 2.0.0" in (tmp_path / "search.py").read_text(encoding="utf-8")
    assert 'ClawdBot-WebSearchPlus/2.0.0"' in (tmp_path / "http_client.py").read_text(encoding="utf-8")
    assert 'EXPECTED_VERSION = "2.0.0"' in (tmp_path / "tests" / "test_release_metadata.py").read_text(encoding="utf-8")
    assert OLD not in (tmp_path / "plugin.yaml").read_text(encoding="utf-8")

    changelog = (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8")
    # New section sits directly under a fresh empty [Unreleased] and inherits
    # the previously unreleased content; the old release section is untouched.
    assert "## [Unreleased]\n\n## [v2.0.0] — 2026-07-04\n\n### Added\n- something new" in changelog
    assert f"## [v{OLD}] — 2026-01-01" in changelog


def test_missing_surface_fails_loudly(tmp_path):
    _make_fake_repo(tmp_path)
    # Simulate surface drift: someone renamed the User-Agent constant.
    (tmp_path / "http_client.py").write_text('UA = "something-else"\n', encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        _run(tmp_path, "2.0.0", "--date", "2026-07-04", "--write")
    assert exc_info.value.code == 2


def test_rejects_same_and_malformed_versions(tmp_path):
    _make_fake_repo(tmp_path)
    assert _run(tmp_path, OLD, "--date", "2026-07-04") == 1
    assert _run(tmp_path, "2.0", "--date", "2026-07-04") == 1
    assert _run(tmp_path, "2.0.0", "--date", "04.07.2026") == 1


def test_rejects_duplicate_changelog_section(tmp_path):
    _make_fake_repo(tmp_path)
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        changelog.read_text(encoding="utf-8") + "\n## [v2.0.0] — 2026-06-01\n\n- already released\n",
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc_info:
        _run(tmp_path, "2.0.0", "--date", "2026-07-04", "--write")
    assert exc_info.value.code == 1


def test_accepts_v_prefix(tmp_path):
    _make_fake_repo(tmp_path)
    assert _run(tmp_path, "v2.0.0", "--date", "2026-07-04", "--write") == 0
    assert 'version: "2.0.0"' in (tmp_path / "plugin.yaml").read_text(encoding="utf-8")


def test_real_repo_surfaces_match_script_table():
    """The SURFACES table must match the actual repo — otherwise the script
    would fail at release time. Runs the planner (read-only) on the real tree."""
    root = Path(__file__).resolve().parents[1]
    current = prepare_release.read_current_version(root)
    changes = prepare_release.plan_surface_updates(root, current, "999.0.0")
    assert changes  # every surface matched exactly once
