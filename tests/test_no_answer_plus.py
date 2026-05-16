from __future__ import annotations

import importlib.util
from pathlib import Path


PLUGIN_PATH = Path(__file__).resolve().parents[1] / "__init__.py"
spec = importlib.util.spec_from_file_location("wsp_plugin_no_answer_under_test", PLUGIN_PATH)
assert spec is not None
wsp = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(wsp)


class FakeCtx:
    def __init__(self):
        self.tools = {}

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs


def test_web_answer_plus_is_not_registered():
    ctx = FakeCtx()

    wsp.register(ctx)

    assert "web_search_plus" in ctx.tools
    assert "web_extract_plus" in ctx.tools
    assert "web_answer_plus" not in ctx.tools


def test_setup_guidance_does_not_advertise_answer_layer(monkeypatch):
    for key in wsp._PROVIDER_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    guidance = wsp._render_setup_guidance({})

    assert "web_answer_plus" not in guidance
    assert "cited answers" not in guidance.lower()
    assert "answer=" not in guidance
