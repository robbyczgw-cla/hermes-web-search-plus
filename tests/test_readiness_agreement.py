"""Status, the tools' availability check and the engine give one answer (5.1.8).

Before 5.1.8 each surface had its own rules. Seen under Hermes: `setup
--keyless-public` printed "Enabled", status said "configured", the tools were
listed, and every search failed with "serper: Missing API key"; running the
suggested setup again said "Nothing changed". These tests load the real plugin,
register its tools, set credentials through a Hermes-style profile resolver (or
the plain environment for standalone hosts) and read what the user and the model
actually get: the status JSON, check_fn, the engine's own decision and the tool
text. Nothing is mocked inside the plugin or engine; no provider is contacted.
"""
from __future__ import annotations

import json
import socket
import stat
import sys

import pytest

from plugin_loader import load_plugin
from wsp_core import config as engine_config
from wsp_core import credentials
from wsp_core.provider_registry import SEARCH_PROVIDER_IDS

wsp = load_plugin("wsp_plugin_readiness_agreement")


class Ctx:
    def __init__(self):
        self.tools = {}

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs

    def __getattr__(self, _name):
        return lambda *a, **k: None


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A clean profile: isolated config.json, no provider env, no network."""
    path = tmp_path / "config.json"
    monkeypatch.setenv("WEB_SEARCH_PLUS_CONFIG", str(path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    for name in wsp._credential_env_names():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(wsp, "_legacy_env_files", lambda: [])

    def no_network(*_a, **_k):
        raise AssertionError("readiness must not contact any provider")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    return path


def _hermes_profile(monkeypatch, values):
    """Install a Hermes-style profile resolver (config.json credentials are then ignored)."""
    monkeypatch.setattr(credentials, "_resolver", lambda name, default=None: values.get(name, default))


def _standalone(monkeypatch):
    monkeypatch.setitem(sys.modules, "agent", None)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", None)
    monkeypatch.setattr(credentials, "_resolver", None)


def _register(monkeypatch):
    # Keep the resolver the test installed (register() would install Hermes' own).
    monkeypatch.setattr(wsp, "_install_hermes_secret_resolver", lambda: None)
    ctx = Ctx()
    wsp.register(ctx)
    return ctx


def _surfaces(ctx):
    config = wsp.load_config()
    status = wsp._provider_config_status(None, config)
    engine_pool = [p for p in SEARCH_PROVIDER_IDS if engine_config.provider_configured(p, config)]
    return status, ctx.tools["web_search_plus"]["check_fn"](), engine_pool


def _search_text(ctx, **args):
    return ctx.tools["web_search_plus"]["handler"]({"query": "python release notes", **args})


def test_keyless_flag_in_config_json_under_hermes_is_named_not_reported_ready(world, monkeypatch):
    world.write_text(json.dumps({"keenable": {"allow_public": True}}))
    _hermes_profile(monkeypatch, {})
    ctx = _register(monkeypatch)

    status, listed, engine_pool = _surfaces(ctx)

    keenable = status["providers"]["keenable"]
    assert (keenable["configured"], keenable["state"]) == (False, "ignored_source")
    assert keenable["fix"][0] == "hermes config set KEENABLE_ALLOW_PUBLIC 1"
    # One answer: status, tool listing and engine agree that nothing can search.
    assert status["search_configured"] is False and listed is False and engine_pool == []
    # A direct call (e.g. a model that kept the tool) gets the fix, not a Serper hint.
    text = _search_text(ctx)
    assert "Keenable's public tier is switched on in config.json, which Hermes ignores" in text
    assert "hermes config set KEENABLE_ALLOW_PUBLIC 1" in text
    assert "serper" not in text.lower()


def test_self_hosted_profile_checks_follow_readiness_under_hermes(world, monkeypatch):
    """The profile block had its own rules: with the SearXNG URL and the Keenable
    opt-in only in config.json, which a Hermes profile ignores, it reported both
    checks ok and policy_ready while the provider rows said ignored_source."""
    _hermes_profile(monkeypatch, {})
    config = {
        "profile": "self_hosted",
        "searxng": {"base_url": "https://8.8.8.8"},
        "keenable": {"allow_public": True},
    }

    payload = wsp._status_payload(env={}, config=config)

    rows = payload["providers"]["providers"]
    assert (rows["searxng"]["state"], rows["keenable"]["state"]) == ("ignored_source", "ignored_source")
    assert {check["id"]: check["ok"] for check in payload["profile"]["checks"]} == {
        "searxng_base_url": False, "keenable_keyless": False,
    }
    assert payload["profile"]["policy_ready"] is False and payload["profile"]["ready"] is False

    # Set where Hermes reads it, the same block is ready.
    in_profile = wsp._status_payload(env={"SEARXNG_INSTANCE_URL": "https://8.8.8.8"}, config={"profile": "self_hosted"})
    assert in_profile["providers"]["providers"]["searxng"]["state"] == "ready"
    assert in_profile["profile"]["checks"][0]["ok"] is True
    assert in_profile["profile"]["policy_ready"] is True and in_profile["profile"]["ready"] is True


def test_profile_opt_in_makes_keenable_ready_everywhere(world, monkeypatch):
    _hermes_profile(monkeypatch, {"KEENABLE_ALLOW_PUBLIC": "1"})
    ctx = _register(monkeypatch)

    status, listed, engine_pool = _surfaces(ctx)

    assert status["providers"]["keenable"]["state"] == "ready"
    assert status["providers"]["keenable"]["source"] == "hermes_profile"
    assert status["search_configured"] is True and listed is True and "keenable" in engine_pool


def test_setup_keyless_under_hermes_prints_the_profile_command_and_writes_nothing(world, monkeypatch, capsys):
    _hermes_profile(monkeypatch, {})
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(["setup", "keenable", "--keyless-public", "--no-jev", "--config-path", str(world)])

    args.func(args)

    out = capsys.readouterr().out
    assert "✓ Enabled keyless public search" not in out
    assert "hermes config set KEENABLE_ALLOW_PUBLIC 1" in out
    assert not (world.exists() and "allow_public" in world.read_text())


def test_setup_keyless_standalone_writes_and_confirms_what_the_engine_uses(world, monkeypatch, capsys):
    _standalone(monkeypatch)
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(["setup", "keenable", "--keyless-public", "--no-jev", "--config-path", str(world)])

    args.func(args)

    out = capsys.readouterr().out
    assert "✓ Enabled keyless public search for Keenable" in out
    assert json.loads(world.read_text())["keenable"]["allow_public"] is True
    assert engine_config.provider_configured("keenable", wsp.load_config())


def test_placeholder_key_keeps_the_tool_and_says_it_is_a_placeholder(world, monkeypatch):
    _hermes_profile(monkeypatch, {"BRAVE_API_KEY": "your-brave-api-key-here"})
    ctx = _register(monkeypatch)

    status, listed, engine_pool = _surfaces(ctx)

    brave = status["providers"]["brave"]
    assert (brave["configured"], brave["state"], brave["reason_code"]) == (False, "invalid_value", "placeholder_value")
    assert engine_pool == []
    # Listed on purpose: the call tells the model what to fix.
    assert listed is True
    text = _search_text(ctx)
    assert "BRAVE_API_KEY still holds a template placeholder" in text
    assert "hermes config set BRAVE_API_KEY" in text


def test_private_searxng_url_names_the_opt_in(world, monkeypatch):
    _standalone(monkeypatch)
    world.write_text(json.dumps({"searxng": {"base_url": "http://127.0.0.1:8888"}}))
    ctx = _register(monkeypatch)

    status, listed, engine_pool = _surfaces(ctx)

    assert status["providers"]["searxng"]["reason_code"] == "searxng_url_private"
    assert engine_pool == [] and listed is True
    assert "SEARXNG_ALLOW_PRIVATE" in _search_text(ctx)


def test_donsetch_binary_from_config_counts_on_both_hosts(world, monkeypatch, tmp_path):
    binary = tmp_path / "donsetch"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    world.write_text(json.dumps({"donsetch": {"binary": str(binary)}}))
    for host in ("hermes", "standalone"):
        if host == "hermes":
            _hermes_profile(monkeypatch, {})
        else:
            _standalone(monkeypatch)
        config = wsp.load_config()
        status = wsp._provider_config_status(None, config)
        donsetch = status["providers"]["donsetch"]
        # 5.1.7: status said configured, the engine said "Missing API key for donsetch".
        assert donsetch["configured"] is True, host
        assert donsetch["state"] == "explicit_only", host
        assert engine_config.validate_api_key("donsetch", config) == str(binary), host


def test_missing_donsetch_binary_is_invalid_not_configured(world, monkeypatch):
    _hermes_profile(monkeypatch, {"DONSETCH_BIN": "/nonexistent/donsetch"})
    status = wsp._provider_config_status(None, wsp.load_config())
    assert status["providers"]["donsetch"]["state"] == "invalid_value"
    assert status["providers"]["donsetch"]["configured"] is False


def test_only_provider_disabled_says_so_instead_of_setup_starter(world, monkeypatch):
    _standalone(monkeypatch)
    monkeypatch.setenv("BRAVE_API_KEY", "brave-real-looking-key-123")
    world.write_text(json.dumps({"auto_routing": {"disabled_providers": ["brave"]}}))
    ctx = _register(monkeypatch)

    text = _search_text(ctx)

    assert "Brave Search is set up but disabled in auto_routing.disabled_providers" in text
    assert "setup --preset starter" not in text


def test_nothing_configured_keeps_the_setup_hint_without_a_serper_line(world, monkeypatch):
    _hermes_profile(monkeypatch, {})
    ctx = _register(monkeypatch)

    status, listed, engine_pool = _surfaces(ctx)
    text = _search_text(ctx)

    assert status["search_configured"] is False and listed is False and engine_pool == []
    assert "hermes web-search-plus setup --preset starter" in text
    # The user never chose Serper; this line used to read like a requirement.
    assert "serper: Missing API key" not in text


def test_session_hint_names_the_ignored_setting(world, monkeypatch, tmp_path):
    world.write_text(json.dumps({"keenable": {"allow_public": True}}))
    _hermes_profile(monkeypatch, {})

    hint = wsp._unconfigured_session_hint(state_path=tmp_path / "hint-state.json")

    assert hint is not None
    assert "Keenable's public tier is switched on in config.json, which Hermes ignores" in hint["message"]
    assert "hermes config set KEENABLE_ALLOW_PUBLIC 1" in hint["message"]


def test_a_real_key_is_ready_on_every_surface(world, monkeypatch):
    _hermes_profile(monkeypatch, {"BRAVE_API_KEY": "brave-real-looking-key-123"})
    ctx = _register(monkeypatch)

    status, listed, engine_pool = _surfaces(ctx)

    assert status["providers"]["brave"]["state"] == "ready"
    assert status["search_configured"] is True and listed is True and "brave" in engine_pool
    assert "brave-real-looking-key-123" not in json.dumps(status)


def test_native_extract_with_only_a_search_key_says_what_to_set_up(world, monkeypatch):
    # Sonnet repro (2026-11-08): web.backend wsp + only a Brave key -> every
    # extract read "WSP extraction failed for this URL" with no hint.
    import importlib.util
    from pathlib import Path
    from types import ModuleType

    abc = ModuleType("agent.web_search_provider")
    abc.WebSearchProvider = object
    web = ModuleType("hermes_cli.config")
    web.load_config = lambda: {"web": {"backend": "wsp"}}
    monkeypatch.setitem(sys.modules, "agent.web_search_provider", abc)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", web)
    _hermes_profile(monkeypatch, {"BRAVE_API_KEY": "brave-real-looking-key-123"})
    spec = importlib.util.spec_from_file_location(
        "_wsp_native_readiness", Path(__file__).resolve().parents[1] / "native_backend.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 0))])

    rows = module.WSPNativeBackend(wsp).extract(["https://example.org/page"])

    assert rows[0]["error"].startswith("WSP: No configured extract provider is available")
    assert "hermes web-search-plus setup --preset extract" in rows[0]["error"]


def test_human_status_starts_with_the_problem_and_its_fix(world, monkeypatch):
    world.write_text(json.dumps({"keenable": {"allow_public": True}}))
    _hermes_profile(monkeypatch, {})

    for fancy in (False, True):
        text = wsp._render_setup_guidance(config=wsp.load_config(), fancy=fancy)
        assert text.splitlines()[0] == (
            "! Keenable: Keenable's public tier is switched on in config.json, which Hermes ignores. "
            "Run: hermes config set KEENABLE_ALLOW_PUBLIC 1"
        )


def test_v3_contract_reports_setup_problems_as_config_errors(world, monkeypatch):
    # MCP clients read this v3 response. 5.1.7: "Missing API key
    # for serper" (the fallback filler), and setup errors could read transient.
    from jsonschema import Draft202012Validator
    from pathlib import Path
    from wsp_core import search

    _standalone(monkeypatch)
    monkeypatch.setenv("BRAVE_API_KEY", "your_brave_key")

    from wsp_core.compat_v3 import legacy_request_to_v3
    from wsp_core.contract_v3 import Capability

    request = legacy_request_to_v3(Capability.SEARCH, {"query": "python release notes", "no_cache": True})
    payload = search.execute_v3_request(request, search._search_adapter(), wsp.load_config()).response.to_dict()

    error = payload["error"]
    assert payload["status"] == "failed"
    assert (error["error_class"], error["retryable"]) == ("config", False)
    assert error["message"].startswith("BRAVE_API_KEY still holds a template placeholder")
    assert error["details"]["readiness"][0]["reason_code"] == "placeholder_value"
    schema = json.loads((Path(__file__).resolve().parents[1] / "schemas/v3/response.schema.json").read_text())
    assert list(Draft202012Validator(schema).iter_errors(payload)) == []
