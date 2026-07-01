from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import provider_registry as registry


GENERATOR_PATH = Path(__file__).resolve().parents[1] / "scripts" / "gen_provider_docs.py"
spec = importlib.util.spec_from_file_location("wsp_gen_provider_docs_under_test", GENERATOR_PATH)
gen = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = gen
spec.loader.exec_module(gen)


def test_provider_docs_have_no_drift_from_registry():
    assert gen.DOC_PATH.exists(), (
        f"docs/PROVIDERS.md is missing; generate it with `{gen.REGENERATE_COMMAND}`."
    )
    assert gen.DOC_PATH.read_text(encoding="utf-8") == gen.render_provider_docs(), (
        "docs/PROVIDERS.md drifted from the provider registry; regenerate it with "
        f"`{gen.REGENERATE_COMMAND}` and commit the result."
    )


def test_provider_docs_cover_every_registered_provider():
    content = gen.DOC_PATH.read_text(encoding="utf-8")

    assert registry.PROVIDER_SPECS, "provider registry unexpectedly empty"
    for provider_spec in registry.PROVIDER_SPECS.values():
        assert provider_spec.display_name in content, provider_spec.provider
        assert provider_spec.env_var in content, provider_spec.provider
        assert f"### {provider_spec.display_name}" in content, provider_spec.provider
        assert provider_spec.description in content, provider_spec.provider
