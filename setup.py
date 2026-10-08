#!/usr/bin/env python3
"""Standalone onboarding and Provider SDK scaffolding for web-search-plus.

Normal users can run ``setup.py status``/``list``/``setup``.  SDK users can
start a self-contained provider with ``setup.py new-provider <id>``.
"""
from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PLUGIN_PATH = ROOT / "__init__.py"
_PROVIDER_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _provider_template(provider_id: str) -> str:
    env_var = provider_id.upper().replace("-", "_") + "_API_KEY"
    config_section = provider_id.replace("-", "_")
    return f'''"""{provider_id} provider for Web Search Plus.

This module is discovered automatically from providers.d.  Keep it
self-contained and import only the stable wsp_sdk surface.
"""

from wsp_sdk import ProviderSpec, register_provider, search_result, source_result


def execute_search(search_module, prov, args, key, config, routing_info):
    """Fetch source results and return a source-only SDK envelope.

    TODO: replace the example item with this provider's HTTP request.  Never
    return a synthesized answer; raise a typed wsp_sdk error for failures.
    """
    return search_result(
        prov,
        args.query,
        [source_result("https://example.invalid/{provider_id}", title="TODO", snippet="TODO")],
    )


PROVIDER = register_provider(
    ProviderSpec(
        id="{provider_id}",
        kind="search",  # Change to "extract" or "both" when needed.
        env_var="{env_var}",
        display_name="{provider_id.title()} (TODO)",
        description="TODO: describe the source-only provider.",
        config_section="{config_section}",
        capability_labels=("search",),
        signup_url="https://example.invalid/signup",  # Replace before release.
        execute_search=execute_search,
        # auto_allowed_by_default=False keeps new providers explicit-only.
    )
)
'''


def _test_template(provider_id: str) -> str:
    test_name = provider_id.replace("-", "_")
    return f'''"""Discovery skeleton for the {provider_id} SDK provider."""


def test_{test_name}_is_discovered():
    from wsp_core import provider_registry

    spec = provider_registry.PROVIDER_SPECS["{provider_id}"]
    assert spec.execute_search is not None
    assert spec.auto_allowed_by_default is False
'''


def _new_provider(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="web-search-plus setup.py new-provider",
        description="Create a self-contained, explicit-only Provider SDK module.",
    )
    parser.add_argument("id", help="lowercase provider id (letters, digits, hyphens)")
    args = parser.parse_args(argv)
    provider_id = args.id.strip().lower()
    if not _PROVIDER_ID_PATTERN.fullmatch(provider_id):
        parser.error("id must use lowercase letters, digits, and single hyphens")

    provider_path = ROOT / "providers.d" / f"{provider_id}.py"
    test_path = ROOT / "tests" / "providers_d" / f"test_{provider_id.replace('-', '_')}.py"
    if provider_path.exists() or test_path.exists():
        parser.error("refusing to overwrite an existing provider module or test skeleton")
    provider_path.parent.mkdir(parents=True, exist_ok=True)
    test_path.parent.mkdir(parents=True, exist_ok=True)
    provider_path.write_text(_provider_template(provider_id), encoding="utf-8")
    test_path.write_text(_test_template(provider_id), encoding="utf-8")
    print(f"Created {provider_path.relative_to(ROOT)} and {test_path.relative_to(ROOT)}")


def _load_plugin():
    """Load the plugin directory as a package, the way Hermes does."""
    name = "web_search_plus_setup"
    spec = importlib.util.spec_from_file_location(name, PLUGIN_PATH, submodule_search_locations=[str(ROOT)])
    plugin = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = plugin
    spec.loader.exec_module(plugin)
    return plugin


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "new-provider":
        _new_provider(sys.argv[2:])
        return
    plugin = _load_plugin()
    parser = argparse.ArgumentParser(
        prog="web-search-plus setup.py",
        description="Configure and inspect web-search-plus provider API keys without Hermes core patches.",
    )
    plugin._web_search_plus_cli_setup(parser)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
