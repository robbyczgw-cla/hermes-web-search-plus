#!/usr/bin/env python3
"""Bump every release-version surface of Web Search Plus in one step.

The repo intentionally keeps one hardcoded release gate
(``tests/test_release_metadata.py: EXPECTED_VERSION``) that asserts all
user-visible version surfaces stay in sync. Historically the bump was done by
hand and missed surfaces twice (v2.8.0 left a stale docstring, v2.9.0 left the
User-Agent and test literals behind, turning CI red on main). This script makes
a half-bump impossible: it rewrites every surface atomically or fails loudly.

Surfaces updated:

- ``plugin.yaml``                        version: "X.Y.Z"
- ``__init__.py``                        __version__ and the header docstring
- ``search.py``                          header docstring "Version: X.Y.Z"
- ``http_client.py``                     DEFAULT_USER_AGENT suffix
- ``operator_console_v3.py``             default Console plugin version
- ``ui.py``                              default server plugin version
- ``tests/test_release_metadata.py``     EXPECTED_VERSION gate
- ``CHANGELOG.md``                       moves [Unreleased] content under a new
                                         "## [vX.Y.Z] — YYYY-MM-DD" section

Usage::

    python3 scripts/prepare_release.py 2.10.0            # dry-run (default)
    python3 scripts/prepare_release.py 2.10.0 --write    # apply changes
    python3 scripts/prepare_release.py 2.10.0 --date 2026-07-04 --write

Exit codes: 0 ok, 1 usage/validation error, 2 a surface pattern was not found
(surface drift — fix the SURFACES table before releasing).
"""

from __future__ import annotations

import argparse
import datetime
import re
import sys
from pathlib import Path
from typing import List, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]

_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# (relative path, pattern template, replacement template). ``{v}`` is the
# version; each pattern must match EXACTLY once with the current version so
# that surface drift is detected instead of silently skipped.
SURFACES: List[Tuple[str, str, str]] = [
    ("plugin.yaml", 'version: "{v}"', 'version: "{v}"'),
    ("__init__.py", '__version__ = "{v}"', '__version__ = "{v}"'),
    ("__init__.py", "Hermes Plugin v{v}", "Hermes Plugin v{v}"),
    ("search.py", "Version: {v}", "Version: {v}"),
    ("http_client.py", 'DEFAULT_USER_AGENT = "ClawdBot-WebSearchPlus/{v}"', 'DEFAULT_USER_AGENT = "ClawdBot-WebSearchPlus/{v}"'),
    ("operator_console_v3.py", 'plugin_version: str = "{v}"', 'plugin_version: str = "{v}"'),
    ("ui.py", 'plugin_version: str = "{v}"', 'plugin_version: str = "{v}"'),
    ("tests/test_release_metadata.py", 'EXPECTED_VERSION = "{v}"', 'EXPECTED_VERSION = "{v}"'),
]

CHANGELOG = "CHANGELOG.md"
UNRELEASED_HEADER = "## [Unreleased]"


def read_current_version(root: Path) -> str:
    """Current release version, from plugin.yaml (the canonical surface)."""
    text = (root / "plugin.yaml").read_text(encoding="utf-8")
    match = re.search(r'^version:\s*"(\d+\.\d+\.\d+)"\s*$', text, re.MULTILINE)
    if not match:
        raise SystemExit("error: could not read current version from plugin.yaml")
    return match.group(1)


def plan_surface_updates(root: Path, old: str, new: str) -> List[Tuple[Path, str, str]]:
    """Return (path, old_text, new_text) per file, or exit 2 on surface drift."""
    updates = {}
    for rel_path, pattern_tpl, replacement_tpl in SURFACES:
        path = root / rel_path
        needle = pattern_tpl.format(v=old)
        replacement = replacement_tpl.format(v=new)
        text = updates.get(path, [None, path.read_text(encoding="utf-8")])[1]
        count = text.count(needle)
        if count != 1:
            print(
                "error: expected exactly 1 occurrence of {!r} in {}, found {} — "
                "update the SURFACES table in scripts/prepare_release.py".format(needle, rel_path, count),
                file=sys.stderr,
            )
            raise SystemExit(2)
        updates[path] = [rel_path, text.replace(needle, replacement)]
    result = []
    for path, (_, new_text) in updates.items():
        result.append((path, path.read_text(encoding="utf-8"), new_text))
    return result


def plan_changelog_update(root: Path, new: str, date: str) -> Tuple[Path, str, str]:
    """Move [Unreleased] content under a new dated release section."""
    path = root / CHANGELOG
    text = path.read_text(encoding="utf-8")
    if text.count(UNRELEASED_HEADER) != 1:
        print(
            "error: expected exactly 1 {!r} header in {}".format(UNRELEASED_HEADER, CHANGELOG),
            file=sys.stderr,
        )
        raise SystemExit(2)
    release_header = "## [v{}] — {}".format(new, date)
    if release_header.split(" — ")[0] in text:
        print("error: CHANGELOG already contains a section for v{}".format(new), file=sys.stderr)
        raise SystemExit(1)
    new_text = text.replace(
        UNRELEASED_HEADER,
        "{}\n\n{}".format(UNRELEASED_HEADER, release_header),
        1,
    )
    return path, text, new_text


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Bump all Web Search Plus release-version surfaces at once.")
    parser.add_argument("version", help="new version, e.g. 2.10.0")
    parser.add_argument("--date", default=None, help="release date YYYY-MM-DD (default: today)")
    parser.add_argument("--write", action="store_true", help="apply changes (default is a dry run)")
    parser.add_argument("--root", default=str(REPO_ROOT), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    root = Path(args.root)
    new = args.version.strip().lstrip("v")
    if not _VERSION_RE.match(new):
        print("error: version must look like MAJOR.MINOR.PATCH, got {!r}".format(args.version), file=sys.stderr)
        return 1
    date = args.date or datetime.date.today().isoformat()
    if not _DATE_RE.match(date):
        print("error: --date must be YYYY-MM-DD, got {!r}".format(args.date), file=sys.stderr)
        return 1

    old = read_current_version(root)
    if new == old:
        print("error: new version {} equals the current version".format(new), file=sys.stderr)
        return 1

    changes = plan_surface_updates(root, old, new)
    changes.append(plan_changelog_update(root, new, date))

    mode = "write" if args.write else "dry-run"
    print("prepare_release: {} -> {} ({})".format(old, new, mode))
    for path, _old_text, new_text in changes:
        print("  {}".format(path.relative_to(root)))
        if args.write:
            path.write_text(new_text, encoding="utf-8")

    if args.write:
        print("done. Now: review the CHANGELOG section, run pytest, commit, tag v{}.".format(new))
    else:
        print("dry run only — re-run with --write to apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
