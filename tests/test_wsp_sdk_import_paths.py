"""Every documented ``wsp_sdk`` import path must resolve to the engine's own objects.

``wsp_sdk`` is the stable public name of the Provider SDK, implemented in
``wsp_core/sdk/``. Provider authors import ``wsp_sdk``, ``wsp_sdk.api``,
``wsp_sdk.errors`` and ``wsp_sdk.conformance``. If any of these were executed a
second time under the public name, the exception and spec classes would be
distinct objects and the engine's ``isinstance`` checks would miss them; the
conformance module would also lose its package context.

Each probe runs in a fresh interpreter in one of the two situations the SDK
must support: the engine loaded the way Hermes does (``hermes_plugins.<slug>``,
repository not on ``sys.path``) and a plain import from the plugin directory.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTED_PATHS = ("wsp_sdk", "wsp_sdk.api", "wsp_sdk.errors", "wsp_sdk.conformance")

HERMES_BOOTSTRAP = r'''
import importlib.util, sys, types

root = sys.argv[1]
namespace = types.ModuleType("hermes_plugins"); namespace.__path__ = []
sys.modules["hermes_plugins"] = namespace
name = "hermes_plugins.web_search_plus"
spec = importlib.util.spec_from_file_location(name, root + "/__init__.py", submodule_search_locations=[root])
module = importlib.util.module_from_spec(spec)
module.__package__ = name
module.__path__ = [root]
sys.modules[name] = module
spec.loader.exec_module(module)
module._load_search_module()
ENGINE = name + ".wsp_core"
'''

PLAIN_BOOTSTRAP = r'''
import sys

sys.path.insert(0, sys.argv[1])
ENGINE = "wsp_core"
'''

CHECKS = r'''
import importlib, json, sys, tempfile
from pathlib import Path

import wsp_sdk  # the public name, imported the way a provider module does
from wsp_sdk import api as api_attribute, conformance as conformance_attribute, errors as errors_attribute
from wsp_sdk.api import ProviderSpec
from wsp_sdk.conformance import assert_provider_conformance, provider_conformance_errors
from wsp_sdk.errors import ProviderConfigError, ProviderContractFailure

engine = lambda dotted: importlib.import_module(ENGINE + dotted)
documented = ["wsp_sdk", "wsp_sdk.api", "wsp_sdk.errors", "wsp_sdk.conformance"]
real = {path: path.replace("wsp_sdk", ".sdk", 1) for path in documented}

# A providers.d-style module that imports the SDK submodules by public name.
providers = Path(tempfile.mkdtemp())
(providers / "probe.py").write_text(
    "from wsp_sdk.api import ProviderSpec\n"
    "PROVIDER = ProviderSpec(id='probe', kind='disabled', env_var='PROBE_API_KEY', "
    "display_name='Probe', description='Probe provider', config_section='probe', "
    "signup_url='https://example.invalid')\n",
    encoding="utf-8",
)
registry = engine(".provider_registry")
specs, diagnostics = registry.discover_providers(providers, existing_ids=registry.PROVIDER_SPECS)

print(json.dumps({
    "same_module": {path: sys.modules[path] is engine(real[path]) for path in documented},
    "imported_via_import_module": {
        path: importlib.import_module(path) is engine(real[path]) for path in documented
    },
    "submodule_attributes": [
        api_attribute is engine(".sdk.api"),
        conformance_attribute is engine(".sdk.conformance"),
        errors_attribute is engine(".sdk.errors"),
    ],
    "api_is_engine": ProviderSpec is engine(".sdk").ProviderSpec,
    "config_error_is_engine": ProviderConfigError is engine(".sdk").ProviderConfigError,
    "contract_failure_is_engine": ProviderContractFailure is engine(".sdk.errors").ProviderContractFailure,
    "top_level_names_are_engine": wsp_sdk.ProviderConfigError is engine(".sdk.errors").ProviderConfigError,
    "conformance": list(provider_conformance_errors()),
    "assert_conformance": assert_provider_conformance() is None,
    "discovered": [spec.provider for spec in specs],
    "discovered_spec_is_engine_class": all(isinstance(spec, engine(".sdk").ProviderSpec) for spec in specs),
    "diagnostics": [(item.module, item.code) for item in diagnostics],
}))
'''


def _run(tmp_path: Path, bootstrap: str) -> dict:
    proc = subprocess.run(
        [sys.executable, "-I", "-c", bootstrap + CHECKS, str(ROOT)],
        cwd=tmp_path, capture_output=True, text=True, timeout=120,
        env={"WSP_CACHE_DIR": str(tmp_path / "cache"), "HERMES_HOME": str(tmp_path / "hermes"),
             "WEB_SEARCH_PLUS_CONFIG": str(tmp_path / "config.json"), "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _assert_engine_objects(result: dict) -> None:
    assert all(result["same_module"].values()), result["same_module"]
    assert all(result["imported_via_import_module"].values()), result["imported_via_import_module"]
    assert result["submodule_attributes"] == [True, True, True]
    assert result["api_is_engine"] is True
    assert result["config_error_is_engine"] is True
    assert result["contract_failure_is_engine"] is True
    assert result["top_level_names_are_engine"] is True
    assert result["conformance"] == []
    assert result["assert_conformance"] is True
    assert result["diagnostics"] == []
    assert result["discovered"] == ["probe"]
    assert result["discovered_spec_is_engine_class"] is True


def test_documented_sdk_paths_resolve_to_the_engine_when_hermes_loads_the_plugin(tmp_path):
    _assert_engine_objects(_run(tmp_path, HERMES_BOOTSTRAP))


def test_documented_sdk_paths_resolve_to_the_engine_on_a_plain_import(tmp_path):
    _assert_engine_objects(_run(tmp_path, PLAIN_BOOTSTRAP))


def test_every_public_sdk_module_is_reachable_under_the_public_name():
    from wsp_core import sdk

    on_disk = sorted(
        path.stem for path in Path(sdk.__file__).parent.glob("*.py") if not path.stem.startswith("_")
    )

    assert on_disk == sorted(sdk._PUBLIC_SUBMODULES)
    assert sorted(f"wsp_sdk.{name}" for name in on_disk) == sorted(DOCUMENTED_PATHS[1:])
