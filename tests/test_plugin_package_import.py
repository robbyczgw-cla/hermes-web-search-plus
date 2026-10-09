"""Hermes loads the plugin as the package ``hermes_plugins.<slug>``.

These probes run in a fresh interpreter (no repository on sys.path), exactly
like a Hermes gateway: they load the plugin the way
``hermes_cli.plugins_loader`` does and check that the engine stays inside the
plugin package - no flat module names, no sys.path edits, no interference with
host modules that share a name with an engine module (``providers``, ``search``).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROBE = r'''
import importlib.util, json, sys, types
from pathlib import Path

root = Path(sys.argv[1])
host_root = Path(sys.argv[2])
sys.path.insert(0, str(host_root))          # the host's own top-level packages
import providers as host_providers           # host module named like an engine module
import search as host_search

engine_names = sorted(p.stem for p in (root / "wsp_core").glob("*.py") if p.stem != "__init__")
before_modules = set(sys.modules)
path_before = list(sys.path)

ns = types.ModuleType("hermes_plugins"); ns.__path__ = []; sys.modules["hermes_plugins"] = ns
name = "hermes_plugins.web_search_plus"
spec = importlib.util.spec_from_file_location(name, root / "__init__.py", submodule_search_locations=[str(root)])
module = importlib.util.module_from_spec(spec)
module.__package__ = name
module.__path__ = [str(root)]
sys.modules[name] = module
spec.loader.exec_module(module)

class Ctx:
    tools = {}
    def register_tool(self, **kw): self.tools[kw["name"]] = kw
    def register_command(self, **kw): pass
    def register_hook(self, *a, **kw): pass

module.register(Ctx())
engine = module._load_search_module()
registry = sys.modules[name + ".wsp_core.provider_registry"]
sdk = sys.modules[name + ".wsp_core.sdk"]
octen = registry.PROVIDER_SPECS.get("octen")
added = sorted(set(sys.modules) - before_modules)
print(json.dumps({
    "version": module.__version__,
    "engine": engine.__name__,
    "tools": sorted(Ctx.tools),
    "flat_engine_modules": [m for m in engine_names if m in added],
    "host_providers_kept": sys.modules["providers"] is host_providers,
    "host_search_kept": sys.modules["search"] is host_search,
    "sys_path_unchanged": sys.path == path_before,
    "wsp_sdk_is_package_sdk": sys.modules.get("wsp_sdk") is sdk,
    "sdk_provider_uses_package_classes": octen is not None and isinstance(octen, sdk.ProviderSpec),
}))
'''


def _plugin_yaml_version() -> str:
    text = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    match = re.search(r'^version:\s*"(\d+\.\d+\.\d+)"\s*$', text, re.MULTILINE)
    assert match, "could not read version from plugin.yaml"
    return match.group(1)


def _probe(tmp_path: Path) -> dict:
    host_root = tmp_path / "host"
    (host_root / "providers").mkdir(parents=True)
    (host_root / "providers" / "__init__.py").write_text("HOST = True\n", encoding="utf-8")
    (host_root / "search.py").write_text("HOST = True\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-I", "-c", PROBE, str(ROOT), str(host_root)],
        cwd=tmp_path, capture_output=True, text=True, timeout=120,
        env={"WSP_CACHE_DIR": str(tmp_path / "cache"), "HERMES_HOME": str(tmp_path / "hermes"),
             "WEB_SEARCH_PLUS_CONFIG": str(tmp_path / "config.json"), "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_hermes_package_load_keeps_the_engine_inside_the_plugin(tmp_path):
    result = _probe(tmp_path)

    assert result["version"] == _plugin_yaml_version()
    assert result["engine"] == "hermes_plugins.web_search_plus.wsp_core.search"
    assert result["tools"] == ["web_extract_plus", "web_search_plus"]
    assert result["flat_engine_modules"] == []
    assert result["host_providers_kept"] is True
    assert result["host_search_kept"] is True
    assert result["sys_path_unchanged"] is True


def test_providers_d_modules_get_the_sdk_of_this_engine(tmp_path):
    result = _probe(tmp_path)

    assert result["wsp_sdk_is_package_sdk"] is True
    assert result["sdk_provider_uses_package_classes"] is True
