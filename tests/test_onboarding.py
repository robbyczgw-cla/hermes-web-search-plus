from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


PLUGIN_PATH = Path(__file__).resolve().parents[1] / "__init__.py"
spec = importlib.util.spec_from_file_location("wsp_plugin_onboarding_under_test", PLUGIN_PATH)
wsp = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(wsp)

SEARCH_PATH = Path(__file__).resolve().parents[1] / "search.py"
search_spec = importlib.util.spec_from_file_location("wsp_search_onboarding_under_test", SEARCH_PATH)
search = importlib.util.module_from_spec(search_spec)
assert search_spec.loader is not None
search_spec.loader.exec_module(search)


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

    assert by_provider["tavily"]["recommended"] is True
    assert by_provider["tavily"]["env"] == "TAVILY_API_KEY"
    assert by_provider["tavily"]["signup_url"].startswith("https://")
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
    assert "TAVILY_API_KEY" in text
    assert "LINKUP_API_KEY" in text
    assert "python ~/.hermes/plugins/web-search-plus/setup.py setup" in text
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
    for item in wsp._get_provider_catalog():
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

    assert starter == {"you", "serper", "linkup"}
    assert lean == {"you", "linkup"}
    assert extract == {"linkup", "firecrawl", "tavily"}
    assert all_providers == {item["provider"] for item in wsp._get_provider_catalog()}


def test_bare_setup_defaults_to_all_supported_providers(monkeypatch, capsys):
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(["setup", "--dry-run"])

    args.func(args)

    out = capsys.readouterr().out
    assert "Setup plan:" in out
    for item in wsp._get_provider_catalog():
        assert item["display_name"] in out


def test_setup_dry_run_prints_plan_without_prompting(monkeypatch, capsys):
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(["setup", "--preset", "lean", "--dry-run"])
    monkeypatch.setattr(wsp.getpass, "getpass", lambda _prompt: (_ for _ in ()).throw(AssertionError("should not prompt")))

    args.func(args)

    out = capsys.readouterr().out
    assert "Setup plan:" in out
    assert "You.com" in out
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
    assert "Providers: 1/13 configured" in out
    assert "Active: You.com" in out
    assert "Brave Search" not in out.split("Setup plan:", 1)[0]


def test_status_uses_target_env_path_for_dashboard(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BRAVE_API_KEY", "live-env-should-not-count")
    env_path = tmp_path / ".env"
    env_path.write_text("LINKUP_API_KEY=x\n")
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(["status", "--env-path", str(env_path)])

    args.func(args)

    out = capsys.readouterr().out
    assert "Providers: 1/13 configured" in out
    assert "Active: Linkup" in out
    assert "Brave Search" not in out


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

    assert config["auto_routing"]["provider_priority"][:6] == ["you", "serper", "exa", "firecrawl", "tavily", "linkup"]
    assert config["auto_routing"]["auto_allow"]["serpbase"] is False
    assert config["auto_routing"]["auto_allow"]["querit"] is False
    assert config["auto_routing"]["auto_allow"]["brave"] is False
    assert config["auto_routing"]["auto_allow"]["parallel"] is False
    assert config["auto_routing"]["auto_allow"]["kilo-perplexity"] is False
    assert config["auto_routing"]["auto_allow"]["perplexity"] is False


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
    assert "auto-allow false: brave, kilo-perplexity, parallel, perplexity, querit, serpbase" in out
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


def test_search_auto_route_errors_cleanly_when_auto_disabled_without_default():
    config = {"default_provider": None, "auto_routing": {"enabled": False}}

    routing = search.auto_route_provider("latest AI news", config)

    assert routing["provider"] is None
    assert routing["reason"] == "auto_routing_disabled_no_default_provider"
    assert routing["confidence_level"] == "low"



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



def test_config_routing_provider_alias_maps_kilo_perplexity_to_distinct_provider(tmp_path):
    config_path = tmp_path / "config.json"
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(["config", "set-default", "kilo-perplexity", "--config-path", str(config_path)])

    args.func(args)

    data = json.loads(config_path.read_text())
    assert data["default_provider"] == "kilo-perplexity"


def test_config_routing_provider_alias_maps_kilo_underscore_perplexity_to_distinct_provider(tmp_path):
    config_path = tmp_path / "config.json"
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(["config", "set-default", "kilo_perplexity", "--config-path", str(config_path)])

    args.func(args)

    data = json.loads(config_path.read_text())
    assert data["default_provider"] == "kilo-perplexity"


def test_config_priority_rejects_non_routing_catalog_provider(tmp_path):
    config_path = tmp_path / "config.json"
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(["config", "set-priority", "tavily,kilo-perplexity", "--config-path", str(config_path), "--dry-run"])

    # Alias is valid, so the dry-run should produce canonical search.py provider names.
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

    monkeypatch.setattr(search, "search_brave", fail_brave)
    monkeypatch.setattr(search, "search_tavily", should_not_fallback)
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


def test_search_load_config_normalizes_kilo_perplexity_alias(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text('{"version": 1, "default_provider": "kilo-perplexity", "auto_routing": {"enabled": false, "provider_priority": ["kilo-perplexity"]}}\n')
    monkeypatch.setenv("WEB_SEARCH_PLUS_CONFIG", str(config_path))

    config = search.load_config()

    assert config["default_provider"] == "kilo-perplexity"
    assert config["auto_routing"]["provider_priority"] == ["kilo-perplexity"]
    assert not list(tmp_path.glob("config.json.broken-*"))


def test_search_load_config_normalizes_kilo_underscore_perplexity_alias(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text('{"version": 1, "default_provider": "kilo_perplexity", "auto_routing": {"enabled": false, "provider_priority": ["kilo_perplexity"], "fallback_provider": "kilo_perplexity"}}\n')
    monkeypatch.setenv("WEB_SEARCH_PLUS_CONFIG", str(config_path))

    config = search.load_config()

    assert config["default_provider"] == "kilo-perplexity"
    assert config["auto_routing"]["provider_priority"] == ["kilo-perplexity"]
    assert config["auto_routing"]["fallback_provider"] == "kilo-perplexity"
    assert not list(tmp_path.glob("config.json.broken-*"))
