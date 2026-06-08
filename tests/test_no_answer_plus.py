from __future__ import annotations

import importlib.util
import sys
import types
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


def test_research_mode_timeout_exceeds_requested_budget():
    # Research mode widens the wall-clock watchdog beyond the requested budget so
    # the in-flight provider/extraction work can finish; normal mode uses the base.
    assert wsp._search_timeout("research", 120) > 120
    assert wsp._search_timeout("normal", 120) == 75


def test_search_runs_in_process_without_subprocess(monkeypatch):
    captured = {}

    def fake_run_search_request(**kwargs):
        captured.update(kwargs)
        return {"provider": "you", "results": []}

    class FakeSearch:
        run_search_request = staticmethod(fake_run_search_request)

    monkeypatch.delenv("WSP_FORCE_SUBPROCESS", raising=False)
    monkeypatch.setattr(wsp, "_load_search_module", lambda: FakeSearch)

    def explode(*args, **kwargs):
        raise AssertionError("subprocess fallback should not be used on the in-process path")

    monkeypatch.setattr(wsp.subprocess, "run", explode)

    result = wsp._run_search("graz weather", mode="research", research_time_budget=120)

    assert result == {"provider": "you", "results": []}
    assert captured["query"] == "graz weather"
    assert captured["mode"] == "research"


def test_research_mode_subprocess_fallback_passes_budget(monkeypatch):
    seen = {}

    def fake_run(cmd, capture_output, text, timeout, env):
        seen["cmd"] = cmd
        seen["timeout"] = timeout

        class Result:
            returncode = 0
            stdout = '{"results": []}'
            stderr = ""

        return Result()

    monkeypatch.setenv("WSP_FORCE_SUBPROCESS", "1")
    monkeypatch.setattr(wsp.subprocess, "run", fake_run)

    wsp._run_search("deep query", mode="research", research_time_budget=120)

    assert "--research-time-budget" in seen["cmd"]
    assert seen["timeout"] > 120


def test_load_search_module_ignores_unrelated_global_search_module(monkeypatch):
    fake_search = types.ModuleType("search")
    setattr(fake_search, "not_the_plugin_engine", True)
    monkeypatch.setitem(sys.modules, "search", fake_search)
    monkeypatch.setattr(wsp, "_search_module", None)
    monkeypatch.setattr(wsp, "_search_import_failed", False)
    monkeypatch.delitem(sys.modules, "_wsp_search_engine", raising=False)

    loaded = wsp._load_search_module()

    assert loaded is not None
    assert loaded is not fake_search
    assert Path(loaded.__file__).resolve() == PLUGIN_PATH.with_name("search.py").resolve()
    assert hasattr(loaded, "run_search_request")
    assert sys.modules["search"] is fake_search
