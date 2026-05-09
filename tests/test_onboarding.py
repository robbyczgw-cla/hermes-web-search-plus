from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path


PLUGIN_PATH = Path(__file__).resolve().parents[1] / "__init__.py"
spec = importlib.util.spec_from_file_location("wsp_plugin_onboarding_under_test", PLUGIN_PATH)
wsp = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(wsp)


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


def test_provider_status_detects_capability_tiers_without_requiring_all(monkeypatch):
    env = {"TAVILY_API_KEY": "tvly-test", "LINKUP_API_KEY": ""}

    status = wsp._provider_config_status(env=env)

    assert status["configured"] is True
    assert status["search_configured"] is True
    assert status["extract_configured"] is True
    assert status["answer_configured"] is True
    assert status["configured_count"] == 1
    assert status["configured_search_count"] == 1
    assert status["configured_extract_count"] == 1
    assert status["providers"]["tavily"]["configured"] is True
    assert status["providers"]["linkup"]["configured"] is False


def test_provider_status_allows_search_only_with_extraction_hint():
    env = {"BRAVE_API_KEY": "brave-test"}

    status = wsp._provider_config_status(env=env)
    text = wsp._render_setup_guidance(env=env)

    assert status["search_configured"] is True
    assert status["extract_configured"] is False
    assert status["answer_configured"] is True
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

    assert starter == {"tavily", "linkup", "brave"}
    assert lean == {"tavily", "linkup"}
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
    assert "Tavily" in out
    assert "Linkup" in out
    assert "Dry run only" in out


def test_setup_dry_run_uses_target_env_path_for_dashboard(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BRAVE_API_KEY", "live-env-should-not-count")
    env_path = tmp_path / ".env"
    env_path.write_text("TAVILY_API_KEY=x\n")
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(["setup", "--preset", "starter", "--dry-run", "--env-path", str(env_path)])

    args.func(args)

    out = capsys.readouterr().out
    assert "Providers: 1/11 configured" in out
    assert "Active: Tavily" in out
    assert "Brave Search" not in out.split("Setup plan:", 1)[0]


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
    assert ctx.tools["web_answer_plus"]["check_fn"]() is False
    assert ctx.tools["web_extract_plus"]["check_fn"]() is False

    monkeypatch.setenv("BRAVE_API_KEY", "brave-test")
    assert ctx.tools["web_search_plus"]["check_fn"]() is True
    assert ctx.tools["web_answer_plus"]["check_fn"]() is True
    assert ctx.tools["web_extract_plus"]["check_fn"]() is False

    monkeypatch.setenv("LINKUP_API_KEY", "linkup-test")
    assert ctx.tools["web_extract_plus"]["check_fn"]() is True
