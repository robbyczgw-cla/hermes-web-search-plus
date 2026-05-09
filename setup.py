#!/usr/bin/env python3
"""Standalone onboarding for web-search-plus.

This intentionally does not depend on Hermes core plugin-CLI support. Normal users can run:

    python ~/.hermes/plugins/web-search-plus/setup.py status
    python ~/.hermes/plugins/web-search-plus/setup.py list
    python ~/.hermes/plugins/web-search-plus/setup.py setup tavily --env-path ~/.hermes/.env
"""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

PLUGIN_PATH = Path(__file__).resolve().parent / "__init__.py"
spec = importlib.util.spec_from_file_location("web_search_plus_plugin_setup", PLUGIN_PATH)
plugin = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(plugin)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="web-search-plus setup.py",
        description="Configure and inspect web-search-plus provider API keys without Hermes core patches.",
    )
    plugin._web_search_plus_cli_setup(parser)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
