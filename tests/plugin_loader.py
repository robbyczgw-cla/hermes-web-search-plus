"""Load the plugin the way Hermes does, sharing the test process's wsp_core.

Hermes imports the plugin directory as a package (``hermes_plugins.<slug>``);
the plugin then imports its engine relatively as ``<package>.wsp_core``. Tests
import the engine as top-level ``wsp_core``. Without help that would give two
copies of every engine module, and a monkeypatch on ``wsp_core.search`` would
not reach the code the tool handlers run. ``load_plugin`` binds the package's
``wsp_core`` to the top-level one before the plugin is executed.
"""

from __future__ import annotations

import importlib
import importlib.util
import pkgutil
import sys
import types
from pathlib import Path

import wsp_core

ROOT = Path(__file__).resolve().parents[1]


def _ensure_parents(name: str) -> None:
    parts = name.split(".")[:-1]
    for index in range(1, len(parts) + 1):
        parent = ".".join(parts[:index])
        if parent not in sys.modules:
            package = types.ModuleType(parent)
            package.__path__ = []  # namespace-like parent, as Hermes creates it
            sys.modules[parent] = package


def _share_engine(package_name: str) -> None:
    for info in pkgutil.walk_packages(wsp_core.__path__, "wsp_core."):
        importlib.import_module(info.name)
    for name, module in list(sys.modules.items()):
        if name == "wsp_core" or name.startswith("wsp_core."):
            sys.modules[f"{package_name}.{name}"] = module


def load_plugin(name: str = "hermes_plugins.web_search_plus"):
    """Import the repository root as plugin package ``name`` and return it."""
    _ensure_parents(name)
    _share_engine(name)
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
