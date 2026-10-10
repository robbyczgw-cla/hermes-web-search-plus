from __future__ import annotations
from wsp_core import providers

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from plugin_loader import load_plugin

from wsp_core import search

wsp = load_plugin("wsp_plugin_onboarding_under_test")


class FakeCtx:
    def __init__(self):
        self.tools = {}
        self.cli_commands = {}
        self.hooks = {}
        self.commands = {}

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs

    def register_cli_command(self, **kwargs):
        self.cli_commands[kwargs["name"]] = kwargs

    def register_hook(self, name, handler):
        self.hooks[name] = handler

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name] = {
            "handler": handler,
            "description": description,
            "args_hint": args_hint,
        }


def test_provider_catalog_has_recommended_starter_metadata():
    catalog = wsp._get_provider_catalog()
    by_provider = {item["provider"]: item for item in catalog}

    assert by_provider["serper"]["recommended"] is True
    assert by_provider["tavily"]["recommended"] is False
    assert by_provider["serper"]["env"] == "SERPER_API_KEY"
    assert by_provider["serper"]["signup_url"].startswith("https://")
    assert "free" in by_provider["linkup"]["free_tier"].lower()
    assert "search" in by_provider["brave"]["capabilities"]


def test_serpbase_catalog_distinguishes_wsp_and_upstream_capabilities():
    catalog = wsp._get_provider_catalog()
    by_provider = {item["provider"]: item for item in catalog}
    serpbase = by_provider["serpbase"]

    assert serpbase["capabilities"] == ["search"]
    assert "news" not in serpbase["capabilities"]
    assert "maps_search" in serpbase["upstream_capabilities"]
    assert "WSP exposes search only" in serpbase["description"]


def test_provider_status_detects_capability_tiers_without_requiring_all(monkeypatch):
    env = {"TAVILY_API_KEY": "tvly-test", "LINKUP_API_KEY": ""}

    status = wsp._provider_config_status(env=env)

    assert status["configured"] is True
    assert status["search_configured"] is True
    assert status["extract_configured"] is True
    assert "answer_configured" not in status
    assert status["configured_count"] == 1
    assert status["configured_search_count"] == 1
    assert status["configured_extract_count"] == 1
    assert status["providers"]["tavily"]["configured"] is True
    assert status["providers"]["linkup"]["configured"] is False


def test_provider_status_treats_template_placeholders_as_missing():
    env = {"SERPER_API_KEY": "***", "LINKUP_API_KEY": "'*****'", "TAVILY_API_KEY": "tvly-test"}

    status = wsp._provider_config_status(env=env)

    assert status["configured"] is True
    assert status["configured_count"] == 1
    assert status["configured_search_count"] == 1
    assert status["configured_extract_count"] == 1
    assert status["providers"]["serper"]["configured"] is False
    assert status["providers"]["linkup"]["configured"] is False
    assert status["providers"]["tavily"]["configured"] is True


def test_read_env_file_omits_template_placeholders(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("SERPER_API_KEY=***\nLINKUP_API_KEY=real-linkup-key\n")

    values = wsp._read_env_file(env_path)

    assert values == {"LINKUP_API_KEY": "real-linkup-key"}


def test_provider_status_allows_search_only_with_extraction_hint():
    env = {"BRAVE_API_KEY": "brave-test"}

    status = wsp._provider_config_status(env=env)
    text = wsp._render_setup_guidance(env=env)

    assert status["search_configured"] is True
    assert status["extract_configured"] is False
    assert "extraction=no" in text
    assert "add LINKUP_API_KEY" in text


def test_setup_guidance_points_unconfigured_users_to_one_simple_path():
    text = wsp._render_setup_guidance(env={})

    assert "web-search-plus is installed but no provider keys are configured" in text
    assert "No single key is mandatory" in text
    assert "extraction-capable" in text
    assert "Recommended starter" in text
    assert "SERPER_API_KEY" in text
    assert "BRAVE_API_KEY" in text
    assert "EXA_API_KEY" in text
    assert "LINKUP_API_KEY" in text
    assert "python3 ~/.hermes/plugins/web-search-plus/setup.py setup" in text
    assert "hermes web-search-plus setup" not in text


def test_env_upsert_writes_selected_provider_keys_without_leaking_values(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("EXISTING=1\nTAVILY_API_KEY=old\n")

    result = wsp._upsert_env_values(env_path, {"TAVILY_API_KEY": "new-secret", "LINKUP_API_KEY": "lk-secret"})

    written = env_path.read_text()
    assert "TAVILY_API_KEY=new-secret" in written
    assert "LINKUP_API_KEY=lk-secret" in written
    assert "EXISTING=1" in written
    assert result == {"updated": ["TAVILY_API_KEY"], "added": ["LINKUP_API_KEY"]}


@pytest.mark.skipif(os.name != "posix", reason="POSIX file permissions")
def test_env_upsert_restricts_env_file_permissions(tmp_path):
    new_env = tmp_path / "new" / ".env"
    wsp._upsert_env_values(new_env, {"TAVILY_API_KEY": "secret"})
    assert (new_env.stat().st_mode & 0o777) == 0o600

    existing = tmp_path / ".env"
    existing.write_text("EXISTING=1\n")
    existing.chmod(0o644)
    wsp._upsert_env_values(existing, {"TAVILY_API_KEY": "secret"})
    assert (existing.stat().st_mode & 0o777) == 0o600


def test_on_session_start_hint_is_one_shot_when_unconfigured(tmp_path):
    state_path = tmp_path / "state.json"

    first = wsp._unconfigured_session_hint(env={}, state_path=state_path)
    second = wsp._unconfigured_session_hint(env={}, state_path=state_path)
    configured = wsp._unconfigured_session_hint(env={"TAVILY_API_KEY": "x"}, state_path=tmp_path / "configured.json")

    assert first is not None
    assert "no provider keys" in first["message"]
    assert second is None
    assert configured is None


def test_standalone_setup_script_lists_providers_without_hermes_core_cli():
    script = Path(__file__).resolve().parents[1] / "setup.py"

    result = subprocess.run(
        [sys.executable, str(script), "list", "--json"],
        check=True,
        text=True,
        capture_output=True,
    )

    assert '"provider": "tavily"' in result.stdout
    assert '"provider": "brave"' in result.stdout


def test_setup_command_treats_eof_as_blank_input(monkeypatch, capsys):
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(["setup"])
    monkeypatch.setattr(wsp.getpass, "getpass", lambda _prompt: (_ for _ in ()).throw(EOFError()))

    args.func(args)

    out = capsys.readouterr().out
    assert "Setup plan:" in out
    for item in wsp._providers_for_preset("starter"):
        assert item["display_name"] in out
    assert "No keys entered; nothing changed." in out


def test_status_dashboard_is_secret_safe_and_actionable():
    text = wsp._render_setup_guidance(env={"BRAVE_API_KEY": "super-secret"}, fancy=True)

    assert "web-search-plus" in text
    assert "✓ search" in text
    assert "• extraction" in text
    assert "super-secret" not in text
    assert "setup.py setup" in text
    assert "setup.py setup --preset starter" not in text


def test_setup_presets_choose_expected_providers():
    starter = {item["provider"] for item in wsp._providers_for_preset("starter")}
    lean = {item["provider"] for item in wsp._providers_for_preset("lean")}
    extract = {item["provider"] for item in wsp._providers_for_preset("extract")}
    all_providers = {item["provider"] for item in wsp._providers_for_preset("all")}

    assert starter == {"brave", "serper", "exa", "linkup"}
    assert lean == {"brave", "linkup"}
    assert extract == {"linkup", "firecrawl", "tavily"}
    assert all_providers == {item["provider"] for item in wsp._get_provider_catalog()}


def test_bare_setup_defaults_to_the_starter_providers(monkeypatch, capsys):
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(["setup", "--dry-run"])

    args.func(args)

    plan = capsys.readouterr().out.split("Setup plan:", 1)[1]
    for item in wsp._providers_for_preset("starter"):
        assert item["display_name"] in plan
    assert "TinyFish" not in plan and "Octen" not in plan


def test_setup_preset_all_still_walks_every_provider(monkeypatch, capsys):
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(["setup", "--preset", "all", "--dry-run"])

    args.func(args)

    plan = capsys.readouterr().out.split("Setup plan:", 1)[1]
    for item in wsp._get_provider_catalog():
        assert item["display_name"] in plan


def test_starter_preset_matches_the_router_first_providers():
    from wsp_core.provider_registry import SETUP_PRESETS
    from wsp_core.routing import INTENT_FIRST_PROVIDER, MEASURED_PROVIDER_ORDER

    router_first = set(INTENT_FIRST_PROVIDER.values()) | {MEASURED_PROVIDER_ORDER[0]}
    assert router_first <= set(SETUP_PRESETS["starter"])
    recommended = {item["provider"] for item in wsp._get_provider_catalog() if item.get("recommended")}
    assert recommended == set(SETUP_PRESETS["starter"])


def test_status_dashboard_names_intents_that_fall_back(monkeypatch):
    status = wsp._provider_config_status(env={"BRAVE_API_KEY": "k1", "SERPER_API_KEY": "k2"})
    text = wsp._render_status_dashboard(status, color=False)
    assert "Routing falls back:" in text
    assert "academic, docs would use Exa (EXA_API_KEY missing)" in text
    full = wsp._provider_config_status(env={"BRAVE_API_KEY": "a", "SERPER_API_KEY": "b", "EXA_API_KEY": "c"})
    assert "Routing falls back" not in wsp._render_status_dashboard(full, color=False)


def test_setup_dry_run_prints_plan_without_prompting(monkeypatch, capsys):
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(["setup", "--preset", "lean", "--dry-run"])
    monkeypatch.setattr(wsp.getpass, "getpass", lambda _prompt: (_ for _ in ()).throw(AssertionError("should not prompt")))

    args.func(args)

    out = capsys.readouterr().out
    assert "Setup plan:" in out
    assert "Brave Search" in out
    assert "Linkup" in out
    assert "Dry run only" in out


def test_setup_dry_run_uses_target_env_path_for_dashboard(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BRAVE_API_KEY", "live-env-should-not-count")
    env_path = tmp_path / ".env"
    env_path.write_text("YOU_API_KEY=x\n")
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(["setup", "--preset", "starter", "--dry-run", "--env-path", str(env_path)])

    args.func(args)

    out = capsys.readouterr().out
    assert "Providers: 1/15 configured" in out
    assert "Active: You.com" in out
    # The live BRAVE_API_KEY must not count; the dashboard reports it missing.
    assert "would use Brave Search (BRAVE_API_KEY missing)" in out.split("Setup plan:", 1)[0]


def test_status_uses_target_env_path_for_dashboard(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BRAVE_API_KEY", "live-env-should-not-count")
    env_path = tmp_path / ".env"
    env_path.write_text("LINKUP_API_KEY=x\n")
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(["status", "--env-path", str(env_path)])

    args.func(args)

    out = capsys.readouterr().out
    assert "Providers: 1/15 configured" in out
    assert "Active: Linkup" in out
    assert "Active: Linkup, Brave" not in out and "Brave Search, " not in out


def test_register_exposes_core_independent_session_onboarding_surfaces():
    ctx = FakeCtx()

    wsp.register(ctx)

    assert "web-search-plus" not in ctx.cli_commands
    assert "web-search-plus-setup" in ctx.commands
    assert "on_session_start" in ctx.hooks


def test_tool_check_functions_treat_missing_or_empty_keys_as_unconfigured(monkeypatch):
    for key in wsp._PROVIDER_ENV_KEYS:
        monkeypatch.setenv(key, "")
    ctx = FakeCtx()

    wsp.register(ctx)

    assert ctx.tools["web_search_plus"]["check_fn"]() is False
    assert "web_answer_plus" not in ctx.tools
    assert ctx.tools["web_extract_plus"]["check_fn"]() is False

    monkeypatch.setenv("BRAVE_API_KEY", "brave-test")
    assert ctx.tools["web_search_plus"]["check_fn"]() is True
    assert ctx.tools["web_extract_plus"]["check_fn"]() is False

    monkeypatch.setenv("LINKUP_API_KEY", "linkup-test")
    assert ctx.tools["web_extract_plus"]["check_fn"]() is True


@pytest.mark.parametrize("removed_key", ["PERPLEXITY_API_KEY", "KILOCODE_API_KEY"])
def test_removed_provider_keys_do_not_configure_search_tool_or_setup_status(monkeypatch, removed_key):
    for key in wsp._PROVIDER_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key in wsp._EXTRACT_PROVIDER_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(removed_key, "legacy-key")
    ctx = FakeCtx()

    wsp.register(ctx)

    assert ctx.tools["web_search_plus"]["check_fn"]() is False
    status = wsp._provider_config_status(env={removed_key: "legacy-key"})
    assert status["search_configured"] is False
    assert status["configured_search_count"] == 0


def test_config_show_json_uses_config_path_without_secrets(tmp_path, capsys):
    config_path = tmp_path / "config.json"
    config_path.write_text('{"version": 1, "default_provider": "brave", "auto_routing": {"enabled": false}}\n')
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(["config", "show", "--json", "--config-path", str(config_path)])

    args.func(args)

    data = json.loads(capsys.readouterr().out)
    assert data["default_provider"] == "brave"
    assert data["auto_routing"]["enabled"] is False


def test_config_set_default_writes_fixed_provider_and_disables_auto(tmp_path):
    config_path = tmp_path / "config.json"
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(["config", "set-default", "brave", "--config-path", str(config_path)])

    args.func(args)

    data = json.loads(config_path.read_text())
    assert data["version"] == 1
    assert data["default_provider"] == "brave"
    assert data["auto_routing"]["enabled"] is False


def test_config_set_routing_on_keeps_default_but_reenables_auto(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text('{"version": 1, "default_provider": "brave", "auto_routing": {"enabled": false}}\n')
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(["config", "set-routing", "on", "--config-path", str(config_path)])

    args.func(args)

    data = json.loads(config_path.read_text())
    assert data["default_provider"] == "brave"
    assert data["auto_routing"]["enabled"] is True


def test_config_set_priority_normalizes_and_dedupes_providers(tmp_path, capsys):
    config_path = tmp_path / "config.json"
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(["config", "set-priority", " Tavily,BRAVE,tavily,linkup ", "--config-path", str(config_path)])

    args.func(args)

    data = json.loads(config_path.read_text())
    assert data["auto_routing"]["provider_priority"][:3] == ["tavily", "brave", "linkup"]
    assert "parallel" in data["auto_routing"]["provider_priority"]
    assert "duplicate provider ignored: tavily" in capsys.readouterr().err


def test_config_disable_and_enable_provider_updates_disabled_list(tmp_path):
    config_path = tmp_path / "config.json"
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    disable = parser.parse_args(["config", "disable", "brave", "--config-path", str(config_path)])
    disable.func(disable)
    enable = parser.parse_args(["config", "enable", "brave", "--config-path", str(config_path)])
    enable.func(enable)

    data = json.loads(config_path.read_text())
    assert "brave" not in data["auto_routing"]["disabled_providers"]


def test_config_set_auto_allow_updates_provider_gate(tmp_path):
    config_path = tmp_path / "config.json"
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    deny = parser.parse_args(["config", "set-auto-allow", "serpbase", "off", "--config-path", str(config_path)])
    deny.func(deny)
    allow = parser.parse_args(["config", "set-auto-allow", "serpbase", "on", "--config-path", str(config_path)])
    allow.func(allow)

    data = json.loads(config_path.read_text())
    assert data["auto_routing"]["auto_allow"]["serpbase"] is True
    assert data["auto_routing"]["auto_allow"]["querit"] is False


def test_default_behavior_config_blocks_low_trust_auto_providers():
    config = wsp._default_behavior_config()

    assert config["auto_routing"]["provider_priority"][:7] == ["brave", "serper", "exa", "tavily", "you", "firecrawl", "linkup"]
    assert config["auto_routing"]["extract_provider_priority"] == list(wsp.EXTRACT_PROVIDER_IDS)
    assert config["auto_routing"]["auto_allow"]["serpbase"] is False
    assert config["auto_routing"]["auto_allow"]["querit"] is False
    assert config["auto_routing"]["auto_allow"].get("brave", True) is True
    assert config["auto_routing"]["auto_allow"].get("parallel", True) is True


def test_setup_dry_run_can_auto_deny_provider(tmp_path, capsys):
    env_path = tmp_path / ".env"
    config_path = tmp_path / "config.json"
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args([
        "setup", "--preset", "starter", "--dry-run", "--env-path", str(env_path), "--config-path", str(config_path),
        "--auto-deny", "serpbase,querit"
    ])

    args.func(args)

    out = capsys.readouterr().out
    assert "auto-allow false: donsetch, octen, querit, serpbase, tinyfish" in out
    assert not env_path.exists()
    assert not config_path.exists()


def test_config_rejects_unknown_provider_without_writing(tmp_path):
    config_path = tmp_path / "config.json"
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(["config", "set-default", "google", "--config-path", str(config_path)])

    try:
        args.func(args)
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("expected SystemExit")
    assert not config_path.exists()


def test_config_dry_run_does_not_write(tmp_path, capsys):
    config_path = tmp_path / "config.json"
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(["config", "set-fallback", "tavily", "--config-path", str(config_path), "--dry-run"])

    args.func(args)

    assert not config_path.exists()
    assert '"fallback_provider": "tavily"' in capsys.readouterr().out


def test_config_reset_creates_backup(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text('{"version": 1, "default_provider": "brave"}\n')
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(["config", "reset", "--config-path", str(config_path), "--yes"])

    args.func(args)

    assert json.loads(config_path.read_text())["default_provider"] is None
    assert list(tmp_path.glob("config.json.bak-*"))


def test_status_json_includes_routing_without_secrets(tmp_path, capsys):
    env_path = tmp_path / ".env"
    env_path.write_text("TAVILY_API_KEY=tvly-secret-value\n")
    config_path = tmp_path / "config.json"
    config_path.write_text('{"version": 1, "default_provider": "tavily", "auto_routing": {"enabled": false}}\n')
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(["status", "--json", "--env-path", str(env_path), "--config-path", str(config_path)])

    args.func(args)

    out = capsys.readouterr().out
    assert "tvly-secret-value" not in out
    data = json.loads(out)
    assert data["routing"]["default_provider"] == "tavily"


def test_search_auto_route_uses_default_provider_when_auto_disabled(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "brave-test-key")
    config = {"default_provider": "brave", "auto_routing": {"enabled": False, "disabled_providers": []}}

    routing = search.auto_route_provider("latest AI news", config)

    assert routing["provider"] == "brave"
    assert routing["reason"] == "auto_routing_disabled_default_provider"
    assert routing["auto_routed"] is False


def test_search_auto_route_falls_back_to_priority_when_auto_disabled_without_default():
    config = {"default_provider": None, "auto_routing": {"enabled": False}}

    routing = search.auto_route_provider("latest AI news", config)

    assert routing["provider"] not in (None, "None")
    assert routing["auto_routed"] is False


def test_config_set_threshold_rejects_out_of_range_without_writing(tmp_path):
    config_path = tmp_path / "config.json"
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(["config", "set-threshold", "2", "--config-path", str(config_path)])

    try:
        args.func(args)
    except SystemExit:
        pass
    else:
        raise AssertionError("expected SystemExit")
    assert not config_path.exists()


def test_corrupt_config_is_moved_aside_and_defaults_are_used(tmp_path, capsys):
    config_path = tmp_path / "config.json"
    config_path.write_text("{ nope")
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(["config", "show", "--json", "--config-path", str(config_path)])

    args.func(args)

    data = json.loads(capsys.readouterr().out)
    assert data["version"] == 1
    assert data["default_provider"] is None
    assert list(tmp_path.glob("config.json.broken-*"))


def test_setup_dry_run_with_routing_preferences_does_not_write(tmp_path, capsys):
    env_path = tmp_path / ".env"
    config_path = tmp_path / "config.json"
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args([
        "setup", "--preset", "starter", "--dry-run", "--env-path", str(env_path), "--config-path", str(config_path),
        "--routing", "fixed", "--default-provider", "brave", "--provider-priority", "brave,tavily,linkup"
    ])

    args.func(args)

    out = capsys.readouterr().out
    assert "auto-routing: off" in out
    assert "default provider: brave" in out
    assert not env_path.exists()
    assert not config_path.exists()


def test_config_show_human_contains_routing_summary(tmp_path, capsys):
    config_path = tmp_path / "config.json"
    config_path.write_text('{"version": 1, "auto_routing": {"enabled": true, "fallback_provider": "linkup"}}\n')
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(["config", "show", "--config-path", str(config_path)])

    args.func(args)

    out = capsys.readouterr().out
    assert "Routing:" in out
    assert "auto-routing: on" in out
    assert "fallback provider: linkup" in out
    assert "search priority:" in out
    assert "extract priority:" in out


def test_config_set_extract_priority_writes_isolated_complete_order(tmp_path):
    config_path = tmp_path / "config.json"
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args([
        "config", "set-extract-priority", "serper,parallel",
        "--config-path", str(config_path),
    ])

    args.func(args)

    data = json.loads(config_path.read_text())
    assert data["auto_routing"]["extract_provider_priority"] == [
        "serper", "parallel", "tavily", "exa", "linkup", "firecrawl", "you", "keenable", "donsetch"
    ]
    assert data["auto_routing"]["provider_priority"] == list(wsp._DEFAULT_PROVIDER_PRIORITY)


def test_config_set_extract_priority_rejects_search_only_provider_without_writing(tmp_path):
    config_path = tmp_path / "config.json"
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args([
        "config", "set-extract-priority", "serper,brave",
        "--config-path", str(config_path),
    ])

    with pytest.raises(SystemExit, match="does not support extraction"):
        args.func(args)

    assert not config_path.exists()


def test_no_secret_leaks_across_status_and_config_commands(tmp_path, capsys):
    secret = "tvly-very-secret-value"
    env_path = tmp_path / ".env"
    env_path.write_text(f"TAVILY_API_KEY={secret}\n")
    config_path = tmp_path / "config.json"
    config_path.write_text('{"version": 1, "default_provider": "tavily", "auto_routing": {"enabled": false}}\n')
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    for argv in [
        ["status", "--json", "--env-path", str(env_path), "--config-path", str(config_path)],
        ["config", "show", "--json", "--config-path", str(config_path)],
        ["config", "show", "--config-path", str(config_path)],
    ]:
        args = parser.parse_args(argv)
        args.func(args)
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err


def test_removed_provider_config_migrates_without_quarantine(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "version": 1,
        "default_provider": "perplexity",
        "perplexity": {"api_key": "legacy"},
        "kilo-perplexity": {"model": "legacy"},
        "auto_routing": {
            "enabled": False,
            "fallback_provider": "kilo_perplexity",
            "provider_priority": ["perplexity", "brave"],
            "extract_provider_priority": ["kilo-perplexity", "exa"],
            "disabled_providers": ["kilo_perplexity"],
            "auto_allow": {"perplexity": True, "kilo-perplexity": False, "brave": True},
        },
    }))
    monkeypatch.setenv("WEB_SEARCH_PLUS_CONFIG", str(config_path))

    config = search.load_config()

    assert config["default_provider"] is None
    assert config["auto_routing"]["fallback_provider"] == "serper"
    assert "perplexity" not in config["auto_routing"]["provider_priority"]
    assert "kilo-perplexity" not in config["auto_routing"]["extract_provider_priority"]
    assert config["auto_routing"]["disabled_providers"] == []
    assert "perplexity" not in config["auto_routing"]["auto_allow"]
    assert "kilo-perplexity" not in config["auto_routing"]["auto_allow"]
    assert "perplexity" not in config and "kilo-perplexity" not in config
    assert not list(tmp_path.glob("config.json.broken-*"))


def test_setup_config_show_migrates_removed_providers_without_quarantine(tmp_path, capsys):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "version": 1,
        "default_provider": "kilo_perplexity",
        "auto_routing": {
            "fallback_provider": "perplexity",
            "provider_priority": ["perplexity", "brave"],
            "extract_provider_priority": ["kilo-perplexity", "exa"],
            "disabled_providers": ["perplexity"],
            "auto_allow": {"kilo-perplexity": True, "brave": True},
        },
    }))
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(["config", "show", "--json", "--config-path", str(config_path)])

    args.func(args)

    shown = json.loads(capsys.readouterr().out)
    assert not list(tmp_path.glob("config.json.broken-*"))
    assert shown["default_provider"] is None
    assert shown["auto_routing"]["fallback_provider"] == "serper"
    assert shown["auto_routing"]["provider_priority"][0] == "brave"
    assert shown["auto_routing"]["extract_provider_priority"][0] == "exa"
    assert shown["auto_routing"]["disabled_providers"] == []
    assert "kilo-perplexity" not in shown["auto_routing"]["auto_allow"]


def test_config_priority_rejects_unknown_provider(tmp_path):
    config_path = tmp_path / "config.json"
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(["config", "set-priority", "tavily,google", "--config-path", str(config_path), "--dry-run"])

    with pytest.raises(SystemExit):
        args.func(args)


def test_invalid_semantic_config_is_moved_aside_and_defaults_are_used(tmp_path, capsys):
    config_path = tmp_path / "config.json"
    config_path.write_text('{"version": 1, "default_provider": "google"}\n')
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(["config", "show", "--json", "--config-path", str(config_path)])

    args.func(args)

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["default_provider"] is None
    assert "invalid config moved" in captured.err
    assert list(tmp_path.glob("config.json.broken-*"))


def test_invalid_threshold_config_is_moved_aside_and_defaults_are_used(tmp_path, capsys):
    config_path = tmp_path / "config.json"
    config_path.write_text('{"version": 1, "auto_routing": {"confidence_threshold": 2}}\n')
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(["status", "--json", "--config-path", str(config_path)])

    args.func(args)

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["routing"]["auto_routing"]["confidence_threshold"] == 0.3
    assert "invalid config moved" in captured.err
    assert list(tmp_path.glob("config.json.broken-*"))


def test_fixed_provider_mode_does_not_add_fallback_providers(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "config.json"
    config_path.write_text('{"version": 1, "default_provider": "brave", "auto_routing": {"enabled": false, "provider_priority": ["tavily"]}}\n')
    monkeypatch.setenv("WEB_SEARCH_PLUS_CONFIG", str(config_path))
    monkeypatch.setenv("BRAVE_API_KEY", "brave-key")
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")
    monkeypatch.setattr(sys, "argv", ["search.py", "--query", "hello", "--provider", "auto", "--no-cache"])

    def fail_brave(**_kwargs):
        raise search.ProviderRequestError("brave down", transient=False)

    def should_not_fallback(**_kwargs):
        raise AssertionError("fixed-provider mode must not call fallback providers")

    monkeypatch.setattr(providers, "search_brave", fail_brave)
    monkeypatch.setattr(providers, "search_tavily", should_not_fallback)
    monkeypatch.setattr(search, "mark_provider_failure", lambda provider, error, retry_after=None: {"cooldown_seconds": 60})

    try:
        search.main()
    except SystemExit as exc:
        assert exc.code == 1
    else:
        raise AssertionError("expected fixed provider failure")

    err = capsys.readouterr().err
    data = json.loads(err)
    assert data["provider"] == "brave"
    assert [item["provider"] for item in data["provider_errors"]] == ["brave"]


def test_search_load_config_quarantines_invalid_default_provider(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text('{"version": 1, "default_provider": "google", "auto_routing": {"enabled": false}}\n')
    monkeypatch.setenv("WEB_SEARCH_PLUS_CONFIG", str(config_path))

    config = search.load_config()

    assert config["default_provider"] is None
    assert config["auto_routing"]["enabled"] is True
    assert list(tmp_path.glob("config.json.broken-*"))


def test_search_load_config_quarantines_invalid_threshold(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text('{"version": 1, "auto_routing": {"confidence_threshold": 2}}\n')
    monkeypatch.setenv("WEB_SEARCH_PLUS_CONFIG", str(config_path))

    config = search.load_config()

    assert config["auto_routing"]["confidence_threshold"] == 0.3
    assert list(tmp_path.glob("config.json.broken-*"))


def test_search_load_config_keeps_multiple_quarantines_in_same_second(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("WEB_SEARCH_PLUS_CONFIG", str(config_path))
    monkeypatch.setattr(search.time, "time", lambda: 12345)

    config_path.write_text('{"version": 1, "default_provider": "google"}\n')
    search.load_config()
    config_path.write_text('{"version": 1, "auto_routing": {"confidence_threshold": 2}}\n')
    search.load_config()

    broken_files = sorted(p.name for p in tmp_path.glob("config.json.broken-*"))
    assert len(broken_files) == 2
    assert broken_files[0] != broken_files[1]


def _isolate_keyless_env(monkeypatch, config_path):
    monkeypatch.setenv("WEB_SEARCH_PLUS_CONFIG", str(config_path))
    for provider in wsp._KEYLESS_PROVIDER_IDS:
        monkeypatch.delenv(wsp.keyless_public_env_var(provider), raising=False)


def test_setup_offers_keyless_public_tier_when_no_key_given(tmp_path, monkeypatch, capsys):
    env_path = tmp_path / ".env"
    config_path = tmp_path / "config.json"
    _isolate_keyless_env(monkeypatch, config_path)
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(["setup", "keenable", "--env-path", str(env_path), "--config-path", str(config_path)])
    monkeypatch.setattr(wsp.getpass, "getpass", lambda _prompt: "")
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")

    args.func(args)

    out = capsys.readouterr().out
    assert "keyless public tier available" in out
    assert "Enabled keyless public search for Keenable" in out
    data = json.loads(config_path.read_text())
    assert data["keenable"]["allow_public"] is True
    assert not env_path.exists()


def test_setup_keyless_public_flag_enables_without_prompting(tmp_path, monkeypatch, capsys):
    env_path = tmp_path / ".env"
    config_path = tmp_path / "config.json"
    _isolate_keyless_env(monkeypatch, config_path)
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(["setup", "keenable", "--keyless-public", "--env-path", str(env_path), "--config-path", str(config_path)])
    monkeypatch.setattr(wsp.getpass, "getpass", lambda _prompt: "")
    monkeypatch.setattr("builtins.input", lambda _prompt: (_ for _ in ()).throw(AssertionError("should not prompt for keyless when flag is set")))

    args.func(args)

    data = json.loads(config_path.read_text())
    assert data["keenable"]["allow_public"] is True


def test_setup_declining_keyless_writes_nothing(tmp_path, monkeypatch, capsys):
    env_path = tmp_path / ".env"
    config_path = tmp_path / "config.json"
    _isolate_keyless_env(monkeypatch, config_path)
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(["setup", "keenable", "--env-path", str(env_path), "--config-path", str(config_path)])
    monkeypatch.setattr(wsp.getpass, "getpass", lambda _prompt: "")
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")

    args.func(args)

    assert "No keys entered; nothing changed." in capsys.readouterr().out
    assert not config_path.exists()


def test_setup_skips_keyless_prompt_when_already_opted_in(tmp_path, monkeypatch, capsys):
    env_path = tmp_path / ".env"
    config_path = tmp_path / "config.json"
    config_path.write_text('{"version": 1, "keenable": {"allow_public": true}}\n')
    _isolate_keyless_env(monkeypatch, config_path)
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(["setup", "keenable", "--env-path", str(env_path), "--config-path", str(config_path)])
    monkeypatch.setattr(wsp.getpass, "getpass", lambda _prompt: "")
    monkeypatch.setattr("builtins.input", lambda _prompt: (_ for _ in ()).throw(AssertionError("should not re-prompt when already opted in")))

    args.func(args)

    assert "No keys entered; nothing changed." in capsys.readouterr().out


class _Stdin:
    def __init__(self, tty: bool):
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def _run_starter_setup(tmp_path, monkeypatch, *, tty: bool, answer: str, config_text: str | None = None):
    """Run `setup --no-jev` with blank key prompts; return the Keenable offer prompts shown."""
    env_path = tmp_path / ".env"
    config_path = tmp_path / "config.json"
    if config_text is not None:
        config_path.write_text(config_text)
    _isolate_keyless_env(monkeypatch, config_path)
    monkeypatch.setattr(wsp.sys, "stdin", _Stdin(tty))
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(["setup", "--no-jev", "--env-path", str(env_path), "--config-path", str(config_path)])
    prompts = []
    monkeypatch.setattr(wsp.getpass, "getpass", lambda _prompt: "")
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or (answer(prompt) if callable(answer) else answer))

    args.func(args)

    return env_path, config_path, prompts


def _allow_public_written(config_path) -> bool:
    return config_path.exists() and "allow_public" in config_path.read_text()


@pytest.mark.parametrize("answer", ["y", "Yes"])
def test_starter_setup_without_any_key_opts_in_to_keyless_start_on_yes(tmp_path, monkeypatch, capsys, answer):
    env_path, config_path, prompts = _run_starter_setup(tmp_path, monkeypatch, tty=True, answer=answer)

    out = capsys.readouterr().out
    assert any("Start without a key using Keenable" in prompt and "[y/N]" in prompt for prompt in prompts)
    assert "Enabled keyless public search for Keenable" in out
    assert json.loads(config_path.read_text())["keenable"]["allow_public"] is True
    assert not env_path.exists()


@pytest.mark.parametrize("answer", ["", "n", "no", "maybe", "  "])
def test_starter_setup_without_any_key_declines_keyless_start_unless_told_yes(tmp_path, monkeypatch, capsys, answer):
    env_path, config_path, prompts = _run_starter_setup(tmp_path, monkeypatch, tty=True, answer=answer)

    assert any("Start without a key using Keenable" in prompt for prompt in prompts)
    assert "No keys entered; nothing changed." in capsys.readouterr().out
    assert not _allow_public_written(config_path)
    assert not env_path.exists()


def test_keyless_offer_says_what_is_sent_before_it_asks(tmp_path, monkeypatch, capsys):
    shown_before_prompt = []

    def answer(prompt):
        shown_before_prompt.append(capsys.readouterr().out)
        return ""

    _run_starter_setup(tmp_path, monkeypatch, tty=True, answer=answer)

    offer = [text for text in shown_before_prompt if "unauthenticated public service" in text]
    assert len(offer) == 1
    assert "queries and fetched URLs" in offer[0]
    assert "Keenable" in offer[0]


def test_starter_setup_without_a_terminal_never_opts_in_to_keyless(tmp_path, monkeypatch, capsys):
    env_path, config_path, prompts = _run_starter_setup(tmp_path, monkeypatch, tty=False, answer="y")

    out = capsys.readouterr().out
    assert not any("Keenable" in prompt for prompt in prompts)
    assert "--keyless-public" in out
    assert "unauthenticated public service" in out
    assert "No keys entered; nothing changed." in out
    assert not _allow_public_written(config_path)
    assert not env_path.exists()


def test_piped_blank_lines_do_not_opt_in_to_keyless(tmp_path):
    script = Path(__file__).resolve().parents[1] / "setup.py"
    env_path = tmp_path / ".env"
    config_path = tmp_path / "config.json"
    clean_env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "HERMES_HOME": str(tmp_path / "hermes"),
        "WEB_SEARCH_PLUS_CONFIG": str(config_path),
    }

    result = subprocess.run(
        [sys.executable, str(script), "setup", "--no-jev", "--env-path", str(env_path), "--config-path", str(config_path)],
        input="\n" * 40,
        env=clean_env,
        check=True,
        text=True,
        capture_output=True,
    )

    assert "--keyless-public" in result.stdout
    assert "No keys entered; nothing changed." in result.stdout
    assert not _allow_public_written(config_path)


def test_starter_setup_with_a_search_key_in_the_process_environment_does_not_offer_keyless(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BRAVE_API_KEY", "brave-env-key-123456")

    env_path, config_path, prompts = _run_starter_setup(tmp_path, monkeypatch, tty=True, answer="y")

    assert not any("Keenable" in prompt for prompt in prompts)
    assert "Start without a key" not in capsys.readouterr().out
    assert not _allow_public_written(config_path)


@pytest.mark.parametrize(
    "config_text",
    [
        '{"version": 1, "keenable": {"api_key": "keenable-config-key-123456"}}\n',
        '{"version": 1, "searxng": {"base_url": "https://search.example"}}\n',
        '{"version": 1, "keenable": {"allow_public": true}}\n',
    ],
    ids=["keenable-key", "searxng-url", "keyless-already-on"],
)
def test_starter_setup_with_a_search_provider_in_the_config_does_not_offer_keyless(tmp_path, monkeypatch, config_text):
    env_path, config_path, prompts = _run_starter_setup(tmp_path, monkeypatch, tty=True, answer="y", config_text=config_text)

    assert not any("Keenable" in prompt for prompt in prompts)


def test_starter_setup_with_a_search_key_in_the_env_file_does_not_offer_keyless(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("EXA_API_KEY=exa-file-key-123456\n")

    env_path, config_path, prompts = _run_starter_setup(tmp_path, monkeypatch, tty=True, answer="y")

    assert not any("Keenable" in prompt for prompt in prompts)
    assert not _allow_public_written(config_path)


def test_starter_setup_with_a_search_key_does_not_offer_keyless(tmp_path, monkeypatch, capsys):
    env_path = tmp_path / ".env"
    config_path = tmp_path / "config.json"
    _isolate_keyless_env(monkeypatch, config_path)
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(["setup", "--no-jev", "--env-path", str(env_path), "--config-path", str(config_path)])
    monkeypatch.setattr(wsp.getpass, "getpass", lambda prompt: "fake-brave-key" if "BRAVE_API_KEY" in prompt else "")
    monkeypatch.setattr("builtins.input", lambda prompt: (_ for _ in ()).throw(AssertionError(prompt)))

    args.func(args)

    out = capsys.readouterr().out
    assert "fake-brave-key" not in out
    assert "BRAVE_API_KEY" in env_path.read_text()
    assert not config_path.exists() or "allow_public" not in config_path.read_text()


def test_empty_dashboard_points_to_keyless_and_donsetch():
    text = wsp._render_setup_guidance(env={}, fancy=True)
    assert "--keyless-public" in text
    assert "DonSeTch" in text and "explicit calls only" in text


def test_routing_rewrite_preserves_non_routing_provider_sections(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text('{"version": 1, "keenable": {"allow_public": true, "search_url": "https://custom"}, "searxng": {"instance_url": "https://x"}}\n')
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(["config", "set-priority", "keenable,brave", "--config-path", str(config_path)])

    args.func(args)

    data = json.loads(config_path.read_text())
    assert data["keenable"] == {"allow_public": True, "search_url": "https://custom"}
    assert data["searxng"] == {"instance_url": "https://x"}
    assert data["auto_routing"]["provider_priority"][:2] == ["keenable", "brave"]
    assert not list(tmp_path.glob("config.json.broken-*"))


def test_fastpath_report_detects_recommended_hermes_config(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "agent:\n"
        "  disabled_toolsets: [web]\n"
    )

    report = wsp._build_fastpath_report(config_path)

    assert report["ok"] is True
    assert report["preferred_web_path_configured"] is True
    checks = {check["id"]: check for check in report["checks"]}
    assert checks["plugin_tools_declared"]["ok"] is True
    assert checks["legacy_web_toolset_disabled"]["ok"] is True


def test_fastpath_report_requires_disabled_toolsets_under_agent(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "other:\n"
        "  disabled_toolsets: [web]\n"
    )

    report = wsp._build_fastpath_report(config_path)

    assert report["preferred_web_path_configured"] is False
    checks = {check["id"]: check for check in report["checks"]}
    assert checks["legacy_web_toolset_disabled"]["ok"] is False


def test_fastpath_report_is_advisory_when_hermes_config_lacks_hints(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agent:\n  disabled_toolsets: []\n")

    report = wsp._build_fastpath_report(config_path)

    assert report["ok"] is True
    assert report["preferred_web_path_configured"] is False
    checks = {check["id"]: check for check in report["checks"]}
    assert checks["legacy_web_toolset_disabled"]["ok"] is False
    assert "agent.disabled_toolsets" in checks["legacy_web_toolset_disabled"]["recommendation"]


def test_fastpath_cli_outputs_json_without_core_patch_dependency(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agent:\n  disabled_toolsets: [web]\n")
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(["fastpath", "--json", "--config-path", str(config_path)])

    args.func(args)

    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert data["recommended_hermes_config"] == {"agent.disabled_toolsets": ["web"]}
    assert "never_defer" not in json.dumps(data)


def test_fastpath_config_path_respects_profile_home(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_CONFIG", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert wsp._get_hermes_config_path() == tmp_path / "config.yaml"
    explicit = tmp_path / "explicit.yaml"
    monkeypatch.setenv("HERMES_CONFIG", str(explicit))
    assert wsp._get_hermes_config_path() == explicit


def test_fastpath_native_selection_keeps_web_enabled(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("web:\n  search_backend: wsp\nagent:\n  disabled_toolsets: []\n")
    report = wsp._build_fastpath_report(path)
    assert report["mode"] == "native"
    assert report["native_capabilities"] == ["search"]
    assert report["preferred_web_path_configured"] is True
    assert report["recommended_hermes_config"] == {}
    assert "disabled_toolsets: [web]" not in wsp._render_fastpath_report(report)


def test_fastpath_native_selection_reports_disabled_web_conflict(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("web:\n  backend: wsp\nagent:\n  disabled_toolsets: [web, browser]\n")
    report = wsp._build_fastpath_report(path)
    assert report["native_capabilities"] == ["search", "extract"]
    assert report["preferred_web_path_configured"] is False
    checks = {row["id"]: row for row in report["checks"]}
    assert checks["native_web_toolset_enabled"]["ok"] is False
    assert "Remove only web" in checks["native_web_toolset_enabled"]["recommendation"]
    assert "disabled_toolsets: [web]" not in wsp._render_fastpath_report(report)


def test_fastpath_native_overrides_and_unrelated_sections(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "other:\n  search_backend: wsp\nweb:\n  backend: wsp\n"
        "  search_backend: searxng\n  extract_backend: 'wsp' # explicit\n"
    )
    report = wsp._build_fastpath_report(path)
    assert report["native_capabilities"] == ["extract"]
    path.write_text("other:\n  backend: wsp\nweb:\n  search_backend: searxng\n")
    assert wsp._build_fastpath_report(path)["mode"] == "plus"


def test_fastpath_report_handles_missing_hermes_config(tmp_path):
    config_path = tmp_path / "missing-config.yaml"

    report = wsp._build_fastpath_report(config_path)

    assert report["ok"] is True
    assert report["preferred_web_path_configured"] is False
    checks = {check["id"]: check for check in report["checks"]}
    assert checks["hermes_config_found"]["ok"] is False
    assert checks["legacy_web_toolset_disabled"]["ok"] is False
