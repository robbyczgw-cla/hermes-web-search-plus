#!/usr/bin/env python3
"""Generate docs/PROVIDERS.md from the provider registry.

Every provider fact in the output comes from provider_registry.py and the
plugin onboarding catalog in __init__.py, so the reference cannot drift from
the single source of truth by hand-editing. Run without arguments to rewrite
the file; run with --check (used by tests/CI) to exit non-zero on drift.
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = ROOT / "docs" / "PROVIDERS.md"
REGENERATE_COMMAND = "python scripts/gen_provider_docs.py"


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module {name} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # Needed so dataclass annotation lookups resolve.
    spec.loader.exec_module(module)
    return module


def _load_registry_and_catalog() -> Tuple[Any, List[Dict[str, Any]]]:
    """Load provider_registry.py and the plugin catalog from __init__.py.

    __init__.py is a Hermes plugin, so it is loaded via spec_from_file_location
    (like tests/test_onboarding.py) instead of a package import. Its fallback
    `from provider_registry import ...` path needs the plugin root on sys.path.
    """
    registry = _load_module("wsp_provider_registry_docgen", ROOT / "provider_registry.py")
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    plugin = _load_module("wsp_plugin_docgen", ROOT / "__init__.py")
    return registry, plugin._get_provider_catalog()


def _capability_mark(supported: bool) -> str:
    return "✅" if supported else "—"


def _keyless_cell(registry: Any, provider: str) -> str:
    if provider not in registry.KEYLESS_PROVIDER_IDS:
        return "—"
    return f"yes (`{registry.keyless_public_env_var(provider)}` opt-in)"


def _auto_routing_cell(registry: Any, provider: str, supports_search: bool) -> str:
    if not supports_search:
        return "—"
    if registry.DEFAULT_AUTO_ALLOW.get(provider) is False:
        return "explicit-only (`auto_allow=false`)"
    priority = list(registry.DEFAULT_PROVIDER_PRIORITY)
    if provider in priority:
        return f"yes (priority {priority.index(provider) + 1})"
    return "yes"


def render_provider_docs() -> str:
    registry, catalog = _load_registry_and_catalog()

    lines: List[str] = [
        "# Provider reference",
        "",
        "<!-- Generated file. Do not edit by hand. -->",
        "",
        "This reference is generated from `provider_registry.py` and the plugin provider",
        f"catalog; regenerate it with `{REGENERATE_COMMAND}` after changing provider metadata.",
        "",
        "## Provider matrix",
        "",
        "| Provider | Search | Extract | Env var | Keyless | Auto routing | Free tier | Signup |",
        "|---|---:|---:|---|---|---|---|---|",
    ]

    for item in catalog:
        spec = registry.PROVIDER_SPECS[item["provider"]]
        lines.append(
            "| {name} | {search} | {extract} | `{env}` | {keyless} | {auto} | {free_tier} | {signup} |".format(
                name=item["display_name"],
                search=_capability_mark(spec.supports_search),
                extract=_capability_mark(spec.supports_extract),
                env=item["env"],
                keyless=_keyless_cell(registry, spec.provider),
                auto=_auto_routing_cell(registry, spec.provider, spec.supports_search),
                free_tier=item["free_tier"],
                signup=item["signup_url"] or "—",
            )
        )

    lines.extend(
        [
            "",
            "`priority N` is the provider's position in the default routing priority list.",
            "Providers marked explicit-only stay out of `provider=\"auto\"` routing and fallback",
            "until opted in with `setup.py config set-auto-allow <provider> on`.",
            "",
            "## Provider notes",
        ]
    )

    for item in catalog:
        lines.extend(["", f"### {item['display_name']}", ""])
        if item["recommended"]:
            lines.extend(["*Recommended starter provider.*", ""])
        lines.append(item["description"])

    lines.append("")
    return "\n".join(lines)


def main(argv: Any = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gen_provider_docs.py",
        description="Generate docs/PROVIDERS.md from the provider registry.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 if docs/PROVIDERS.md drifted from the registry instead of rewriting it.",
    )
    args = parser.parse_args(argv)

    content = render_provider_docs()
    relative_doc = DOC_PATH.relative_to(ROOT)

    if args.check:
        existing = DOC_PATH.read_text(encoding="utf-8") if DOC_PATH.exists() else None
        if existing != content:
            print(f"{relative_doc} is out of date with the provider registry.", file=sys.stderr)
            print(f"Regenerate it with: {REGENERATE_COMMAND}", file=sys.stderr)
            return 1
        print(f"{relative_doc} is up to date.")
        return 0

    DOC_PATH.parent.mkdir(parents=True, exist_ok=True)
    DOC_PATH.write_text(content, encoding="utf-8")
    print(f"Wrote {relative_doc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
