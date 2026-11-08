"""DonSeTch failures, ``setup --no-jev`` and the engine-load retry, at the user's boundary.

Everything runs through the registered Hermes tool handlers, the real setup/status
CLI and the real config file. Only the DonSeTch binary (a small stdio script that
speaks MCP) is simulated.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import socket
import stat
import sys
import textwrap

import pytest

from plugin_loader import load_plugin
from wsp_core import extract

plugin = load_plugin("wsp_plugin_donsetch_setup_under_test")

_FAKE = textwrap.dedent(
    '''\
    #!{python}
    import json, sys, time
    for line in sys.stdin:
        m = json.loads(line)
        if "id" not in m:
            continue
        if m["method"] == "initialize":
            out = {{"jsonrpc": "2.0", "id": m["id"], "result": {{"capabilities": {{"tools": {{}}}}}}}}
        elif {hang}:
            time.sleep(600)
        elif m["params"]["name"] == "web_search":
            res = [{{"rank": 1, "url": "https://a.example/1", "title": "One", "snippet": "first"}}]
            out = {{"jsonrpc": "2.0", "id": m["id"], "result": {{
                "structuredContent": {{"results": res, "engines": [], "intent": "web"}},
                "content": [{{"type": "text", "text": "ok"}}]}}}}
        else:
            u = m["params"]["arguments"].get("url", "")
            out = {{"jsonrpc": "2.0", "id": m["id"], "result": {{
                "structuredContent": {{"url": u, "content_ok": True, "status": 200}},
                "content": [{{"type": "text", "text": "page body for " + u}}]}}}}
        print(json.dumps(out), flush=True)
    '''
)


def _fake_binary(tmp_path, *, hang=False):
    path = tmp_path / ("donsetch_hang" if hang else "donsetch_ok")
    path.write_text(_FAKE.format(python=sys.executable, hang="True" if hang else "False"), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


class _Ctx:
    def __init__(self):
        self.tools = {}

    def register_tool(self, name, **kwargs):
        self.tools[name] = kwargs

    def register_hook(self, *_args, **_kwargs):
        pass


@pytest.fixture
def tools(tmp_path, monkeypatch):
    """The tools as Hermes registers them, with a temp config and no real keys."""
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"donsetch": {"timeout": 5}}), encoding="utf-8")
    monkeypatch.setenv("WEB_SEARCH_PLUS_CONFIG", str(config))
    real = socket.getaddrinfo
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda host, *a, **k: real("93.184.216.34" if str(host).endswith(".example") else host, *a, **k),
    )
    ctx = _Ctx()
    plugin.register(ctx)
    return ctx.tools


def _extract(tools, binary, monkeypatch, **extra):
    monkeypatch.setenv("DONSETCH_BIN", binary)
    args = {"urls": ["https://a.example/x"], "provider": "donsetch", **extra}
    return tools["web_extract_plus"]["handler"](args)


def _search(tools, binary, monkeypatch, **extra):
    monkeypatch.setenv("DONSETCH_BIN", binary)
    args = {"query": "tokio", "provider": "donsetch", **extra}
    return tools["web_search_plus"]["handler"](args)


# -- binary cannot be started: a reason and a fix, not "Provider execution failed" -------------------


def test_binary_not_on_path_extract_names_the_fix(tools, monkeypatch):
    # A bare name passes the readiness check (it is resolved at spawn time) but is not on PATH.
    text = _extract(tools, "donsetch-not-installed", monkeypatch)
    assert "DONSETCH_BIN" in text
    assert "could not read this page" not in text


def test_binary_not_on_path_search_names_the_fix(tools, monkeypatch):
    text = _search(tools, "donsetch-not-installed", monkeypatch)
    assert "DONSETCH_BIN" in text
    assert "Provider execution failed" not in text


def test_binary_not_on_path_is_a_config_error_in_the_engine(tools, monkeypatch):
    monkeypatch.setenv("DONSETCH_BIN", "donsetch-not-installed")
    result = extract.extract_plus(["https://a.example/x"], provider="donsetch")
    assert result["fallback_errors"][0]["error"] == "config"


# -- timeouts are timeouts ---------------------------------------------------------------------------


def test_hung_binary_is_a_timeout_not_internal(tools, tmp_path, monkeypatch):
    binary = _fake_binary(tmp_path, hang=True)
    text = _extract(tools, binary, monkeypatch)
    assert "timed out" in text
    result = extract.extract_plus(["https://a.example/x"], provider="donsetch")
    assert result["fallback_errors"][0]["error"] == "timeout"


# -- options DonSeTch cannot apply: run without them and say so --------------------------------------


def test_search_with_freshness_runs_and_reports_not_applied(tools, tmp_path, monkeypatch):
    text = _search(tools, _fake_binary(tmp_path), monkeypatch, freshness="day")
    assert "https://a.example/1" in text
    assert "[Freshness: day | not applied" in text
    assert "All providers failed" not in text


def test_search_with_time_range_runs_and_reports_not_applied(tools, tmp_path, monkeypatch):
    text = _search(tools, _fake_binary(tmp_path), monkeypatch, time_range="week")
    assert "https://a.example/1" in text
    assert "not applied" in text


def test_extract_html_format_returns_markdown_and_says_so(tools, tmp_path, monkeypatch):
    text = _extract(tools, _fake_binary(tmp_path), monkeypatch, format="html")
    assert "page body for https://a.example/x" in text
    assert "[Format: markdown" in text


# -- setup --no-jev ----------------------------------------------------------------------------------


def _cli(argv, capsys):
    parser = argparse.ArgumentParser()
    plugin._web_search_plus_cli_setup(parser)
    args = parser.parse_args(argv)
    with contextlib.redirect_stderr(io.StringIO()):
        args.func(args)
    return capsys.readouterr().out


@pytest.fixture
def jev_config(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"jev": {"enabled": True, "search_type": True, "extract_quality": True, "language_fill": True}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("WEB_SEARCH_PLUS_CONFIG", str(path))
    monkeypatch.setenv("BRAVE_API_KEY", "dummy-key-for-readiness")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    return path


def test_no_jev_turns_an_enabled_jev_off(jev_config, capsys):
    out = _cli(["setup", "--no-jev", "--default-provider", "brave"], capsys)
    saved = json.loads(jev_config.read_text(encoding="utf-8"))["jev"]
    assert saved["enabled"] is False
    assert not any(saved[name] for name in ("search_type", "extract_quality", "language_fill"))
    assert "switch Jev off" in out
    status = json.loads(_cli(["status", "--json"], capsys))
    assert status["jev"]["enabled"] is False


def test_setup_without_jev_flag_keeps_an_enabled_jev(jev_config, capsys):
    _cli(["setup", "--default-provider", "brave"], capsys)
    assert json.loads(jev_config.read_text(encoding="utf-8"))["jev"]["enabled"] is True


def test_no_jev_when_already_off_changes_nothing(tmp_path, monkeypatch, capsys):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"default_provider": "brave"}), encoding="utf-8")
    monkeypatch.setenv("WEB_SEARCH_PLUS_CONFIG", str(path))
    monkeypatch.setenv("BRAVE_API_KEY", "dummy-key-for-readiness")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    out = _cli(["setup", "--no-jev"], capsys)
    assert "leave Jev disabled" in out
    assert "jev" not in json.loads(path.read_text(encoding="utf-8"))


# -- engine load: retried after a cooldown, tools hidden meanwhile ------------------------------------


@pytest.fixture
def flaky_engine(monkeypatch):
    real = plugin.importlib.import_module
    state = {"fail": True, "calls": 0}

    def flaky(name, package=None):
        if name.endswith("wsp_core.search"):
            state["calls"] += 1
            if state["fail"]:
                raise OSError("file is being replaced")
        return real(name, package)

    monkeypatch.setattr(plugin.importlib, "import_module", flaky)
    monkeypatch.setattr(plugin, "_search_module", None)
    monkeypatch.setattr(plugin, "_search_import_failed", False)
    monkeypatch.setattr(plugin, "_search_import_failed_at", None, raising=False)
    clock = {"now": 1000.0}
    monkeypatch.setattr(plugin.time, "monotonic", lambda: clock["now"])
    return state, clock


def test_engine_import_failure_is_retried_after_cooldown(tools, flaky_engine, monkeypatch):
    state, clock = flaky_engine
    monkeypatch.setenv("BRAVE_API_KEY", "dummy-key-for-readiness")
    assert plugin._load_search_module() is None
    text = tools["web_search_plus"]["handler"]({"query": "x"})
    assert "failed to load" in text
    assert state["calls"] == 1  # inside the window the import is not tried again
    state["fail"] = False
    clock["now"] += plugin._ENGINE_RETRY_SECONDS + 1
    assert plugin._load_search_module() is not None
    assert state["calls"] == 2


def test_tools_are_not_listed_while_the_engine_cannot_load(tools, flaky_engine, monkeypatch):
    state, clock = flaky_engine
    monkeypatch.setenv("BRAVE_API_KEY", "dummy-key-for-readiness")
    assert tools["web_search_plus"]["check_fn"]() is True  # not tried yet: listed
    assert plugin._load_search_module() is None
    assert tools["web_search_plus"]["check_fn"]() is False
    assert tools["web_extract_plus"]["check_fn"]() is False
    state["fail"] = False
    clock["now"] += plugin._ENGINE_RETRY_SECONDS + 1
    assert tools["web_search_plus"]["check_fn"]() is True
