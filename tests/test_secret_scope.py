"""Credentials come from the host's profile scope, never from .env files (catalog rule 11).

Hermes installs ``agent.secret_scope.get_secret`` as the engine's resolver. Its
scope is a contextvar, so the engine's worker threads must run in the caller's
context, and a refused read (``UnscopedSecretError`` under multiplexing) must
reach the tool output instead of falling back to ``os.environ``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
import types
from contextvars import ContextVar
from pathlib import Path

import pytest

from plugin_loader import load_plugin
from wsp_core import credentials, providers, search
from wsp_core.config import _deepcopy_default_config

plugin = load_plugin("wsp_plugin_test_secret_scope")
ROOT = Path(__file__).resolve().parents[1]

_SCOPE: ContextVar = ContextVar("test_secret_scope", default=None)


class UnscopedSecretError(RuntimeError):
    """Stand-in for agent.secret_scope.UnscopedSecretError."""


def scoped_get_secret(name, default=None):
    """Like Hermes under multiplexing: no scope bound means fail closed."""
    scope = _SCOPE.get()
    if scope is None:
        raise UnscopedSecretError(f"Hermes could not read this profile's {name}")
    return scope.get(name, default)


@pytest.fixture(autouse=True)
def _unbound_scope():
    token = _SCOPE.set(None)
    yield
    _SCOPE.reset(token)


@pytest.fixture
def hermes(monkeypatch):
    """Make ``from agent.secret_scope import get_secret`` resolve to the stand-in."""
    agent = types.ModuleType("agent")
    agent.__path__ = []
    scope_module = types.ModuleType("agent.secret_scope")
    scope_module.get_secret = scoped_get_secret
    scope_module.UnscopedSecretError = UnscopedSecretError
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", scope_module)
    return scope_module


class _Ctx:
    def __init__(self):
        self.tools = {}

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs


def _register():
    ctx = _Ctx()
    plugin.register(ctx)
    return ctx


def _route_to(monkeypatch, provider):
    monkeypatch.setattr(search, "auto_route_provider", lambda query, config: {
        "provider": provider, "confidence": 0.9, "confidence_level": "high", "reason": "test",
        "routing_policy": "routing-v3", "scores": {provider: 9.0}, "top_signals": [],
        "analysis_summary": {"routing_class": "general"},
    })


def _config():
    config = _deepcopy_default_config()
    config["auto_routing"]["provider_priority"] = ["serper", "brave"]
    config["v3"] = {"hedge_min_delay_seconds": 0.2, "attempt_timeout_seconds": 5}
    return config


def test_register_installs_the_hermes_resolver(hermes):
    _register()
    assert credentials.get_secret_resolver() is scoped_get_secret


def test_register_without_secret_scope_falls_back_to_process_env(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "agent.secret_scope", None)  # import raises ImportError
    monkeypatch.setattr(plugin, "_HERMES_RESOLVER_FALLBACK_LOGGED", False)
    credentials.set_secret_resolver(scoped_get_secret)
    with caplog.at_level("INFO"):
        _register()
        _register()
    assert credentials.get_secret_resolver() is None
    assert sum("no agent.secret_scope" in r.getMessage() for r in caplog.records) == 1


def test_concurrent_profiles_each_see_only_their_own_key(monkeypatch, hermes):
    """(a) Two profiles search at the same time; the fallback runs on a hedge worker thread."""
    _register()
    _route_to(monkeypatch, "serper")
    # A key in the process environment belongs to neither profile.
    monkeypatch.setenv("BRAVE_API_KEY", "process-env-brave-key-000")
    both_started = threading.Barrier(2, timeout=10)
    seen = {}

    def empty_serper(query, api_key, max_results=5, **_kwargs):
        both_started.wait()  # both profiles are inside the engine at once
        return {"provider": "serper", "query": query, "results": []}

    def brave(query, api_key, max_results=5, **_kwargs):
        seen.setdefault(query, []).append({
            "api_key": api_key,
            "worker_reads": credentials.get_secret("BRAVE_API_KEY"),
            "thread": threading.current_thread().name,
        })
        return {"provider": "brave", "query": query, "results": [
            {"title": "t", "url": f"https://example.com/{query}", "snippet": "s"}]}

    monkeypatch.setattr(providers, "search_serper", empty_serper)
    monkeypatch.setattr(providers, "search_brave", brave)
    results = {}

    def profile(name):
        _SCOPE.set({
            "SERPER_API_KEY": f"{name}-serper-key-123456",
            "BRAVE_API_KEY": f"{name}-brave-key-1234567",
        })
        results[name] = plugin._call_with_timeout(
            lambda: search.run_search_request(
                query=name, no_cache=True, config=_config(), provider="auto"), 30)

    threads = [threading.Thread(target=profile, args=(name,)) for name in ("alpha", "beta")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    for name in ("alpha", "beta"):
        assert results[name]["provider"] == "brave", results[name]
        assert results[name]["routing"]["fallback_used"] is True
        assert seen[name] == [{
            "api_key": f"{name}-brave-key-1234567",
            "worker_reads": f"{name}-brave-key-1234567",
            "thread": "wsp-daemon-task",
        }]


def test_refused_read_is_a_clear_error_without_env_fallback(monkeypatch, hermes):
    """(b) No scope bound: the tool says why, and the os.environ key is never used."""
    ctx = _register()
    monkeypatch.setenv("BRAVE_API_KEY", "process-env-brave-key-000")
    called = []
    monkeypatch.setattr(providers, "search_brave", lambda *a, **k: called.append(k) or {})

    output = ctx.tools["web_search_plus"]["handler"]({"query": "unscoped", "provider": "brave"})

    assert "Hermes could not read this profile's" in output
    assert "not configured" not in output
    assert called == []


def test_refused_read_in_auto_routing_is_not_reported_as_missing_keys(monkeypatch, hermes):
    ctx = _register()
    monkeypatch.setenv("BRAVE_API_KEY", "process-env-brave-key-000")
    output = ctx.tools["web_search_plus"]["handler"]({"query": "unscoped auto"})
    assert "Hermes could not read this profile's" in output

    extract = ctx.tools["web_extract_plus"]["handler"]({"urls": ["https://example.com"]})
    assert "Hermes could not read this profile's" in extract


def test_refused_read_propagates_from_check_fn(hermes):
    ctx = _register()
    with pytest.raises(UnscopedSecretError):
        ctx.tools["web_search_plus"]["check_fn"]()
    with pytest.raises(UnscopedSecretError):
        ctx.tools["web_extract_plus"]["check_fn"]()


def test_check_fn_sees_keys_through_the_resolver(monkeypatch, hermes, tmp_path):
    """(d) Keys only in the profile scope (not os.environ) make the tools available."""
    monkeypatch.setenv("WEB_SEARCH_PLUS_CONFIG", str(tmp_path / "missing-config.json"))
    ctx = _register()
    search_check = ctx.tools["web_search_plus"]["check_fn"]
    extract_check = ctx.tools["web_extract_plus"]["check_fn"]

    _SCOPE.set({})
    assert search_check() is False
    assert extract_check() is False

    _SCOPE.set({"BRAVE_API_KEY": "profile-brave-key-1234567"})
    assert search_check() is True
    assert extract_check() is False

    _SCOPE.set({"FIRECRAWL_API_KEY": "profile-firecrawl-key-123"})
    assert extract_check() is True

    _SCOPE.set({"KEENABLE_ALLOW_PUBLIC": "1"})
    assert search_check() is True
    assert extract_check() is True


def test_jev_key_comes_from_the_resolver_and_key_files_are_not_read(monkeypatch, tmp_path):
    from wsp_core import jev_optional

    key_file = tmp_path / "typesafe_api_key"
    key_file.write_text("file-key", encoding="utf-8")
    monkeypatch.setenv("TYPESAFE_API_KEY_FILE", str(key_file))
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert jev_optional._key() == ""

    credentials.set_secret_resolver(lambda name, default=None: {"TYPESAFE_API_KEY": " profile-key "}.get(name, default))
    assert jev_optional._key() == "profile-key"


_NO_DOTENV_PROBE = textwrap.dedent(
    """
    import json, sys
    opened = []

    def audit(event, args):
        if event == "open" and args and str(args[0]).endswith(".env"):
            opened.append(str(args[0]))

    sys.addaudithook(audit)
    sys.path.insert(0, sys.argv[1])
    sys.path.insert(0, sys.argv[1] + "/tests")
    mode = sys.argv[2]
    if mode == "plugin":
        from plugin_loader import load_plugin

        plugin = load_plugin("hermes_plugins.web_search_plus")

        class Ctx:
            tools = {}

            def register_tool(self, **kwargs):
                self.tools[kwargs["name"]] = kwargs

        ctx = Ctx()
        plugin.register(ctx)
        available = ctx.tools["web_search_plus"]["check_fn"]()
        output = ctx.tools["web_search_plus"]["handler"]({"query": "probe", "provider": "brave"})
    else:
        import runpy
        sys.argv = ["search.py", "--query", "probe", "--provider", "brave"]
        try:
            runpy.run_path(sys.path[1] + "/search.py", run_name="__main__")
        except SystemExit:
            pass
        available = None
        output = None
    print(json.dumps({"opened": opened, "available": available, "output": output}), file=sys.stderr)
    """
)


@pytest.mark.parametrize("mode", ["plugin", "cli"])
def test_no_dotenv_file_is_ever_opened(tmp_path, mode):
    """(c) Neither importing the plugin, check_fn, a tool call nor the CLI reads .env files.

    The Hermes profile .env holds a key; it must not be picked up.
    """
    home = tmp_path / "hermes-home"
    home.mkdir()
    (home / ".env").write_text("BRAVE_API_KEY=dotenv-brave-key-1234567\n", encoding="utf-8")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "HERMES_HOME": str(home),
        "WSP_CACHE_DIR": str(tmp_path / "cache"),
        "WEB_SEARCH_PLUS_CONFIG": str(tmp_path / "missing-config.json"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    proc = subprocess.run(
        [sys.executable, "-c", _NO_DOTENV_PROBE, str(ROOT), mode],
        capture_output=True, text=True, env=env, timeout=60, cwd=str(tmp_path),
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stderr.strip().splitlines()[-1])
    output = report["output"] if mode == "plugin" else proc.stdout + proc.stderr
    assert report["opened"] == []
    assert "dotenv-brave-key" not in output
    assert "not configured" in output
    if mode == "plugin":
        assert report["available"] is False


def test_native_backend_reports_a_refused_read(monkeypatch, hermes):
    """The opt-in native wsp backend passes the scope error on instead of a generic failure."""
    web_provider = types.ModuleType("agent.web_search_provider")
    web_provider.WebSearchProvider = object
    web_tools = types.ModuleType("hermes_cli.config")
    web_tools.load_config = lambda: {"web": {"backend": "wsp"}}
    monkeypatch.setitem(sys.modules, "agent.web_search_provider", web_provider)
    monkeypatch.setitem(sys.modules, "tools", types.ModuleType("tools"))
    monkeypatch.setitem(sys.modules, "hermes_cli.config", web_tools)
    import importlib

    # register() in an earlier test may have imported the backend against a real
    # Hermes (its venv has agent/ and tools/); import it fresh against the stand-ins.
    monkeypatch.delitem(sys.modules, f"{plugin.__name__}.native_backend", raising=False)
    native = importlib.import_module(".native_backend", plugin.__name__)
    _register()
    backend = native.WSPNativeBackend(plugin)

    with pytest.raises(UnscopedSecretError):
        backend.is_available()
    assert "Hermes could not read this profile's" in backend.search("unscoped")["error"]
    assert "Hermes could not read this profile's" in backend.extract(["https://example.com"])[0]["error"]

    _SCOPE.set({"BRAVE_API_KEY": "profile-brave-key-1234567"})
    assert backend.is_available() is True
