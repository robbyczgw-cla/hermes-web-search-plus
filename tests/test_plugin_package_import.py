from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_plugin_loads_with_hermes_package_style_import():
    parent_name = "hermes_plugins"
    module_name = f"{parent_name}.web_search_plus_import_test"

    sys.modules.pop(module_name, None)
    previous_parent = sys.modules.get(parent_name)
    if previous_parent is None:
        parent = types.ModuleType(parent_name)
        parent.__path__ = []  # type: ignore[attr-defined]
        sys.modules[parent_name] = parent

    try:
        spec = importlib.util.spec_from_file_location(
            module_name,
            ROOT / "__init__.py",
            submodule_search_locations=[str(ROOT)],
        )
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        module.__package__ = module_name
        module.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
        sys.modules[module_name] = module

        spec.loader.exec_module(module)

        assert module.__version__ == "2.5.1"
        assert module._get_provider_catalog()
    finally:
        sys.modules.pop(module_name, None)
        if previous_parent is None:
            sys.modules.pop(parent_name, None)
