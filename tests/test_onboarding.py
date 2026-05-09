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


def test_provider_status_detects_any_configured_key_without_requiring_all(monkeypatch):
    env = {"TAVILY_API_KEY": "tvly-test", "LINKUP_API_KEY": ""}

    status = wsp._provider_config_status(env=env)

    assert status["configured"] is True
    assert status["configured_count"] == 1
    assert status["providers"]["tavily"]["configured"] is True
    assert status["providers"]["linkup"]["configured"] is False


def test_setup_guidance_points_unconfigured_users_to_one_simple_path():
    text = wsp._render_setup_guidance(env={})

    assert "web-search-plus is installed but no provider keys are configured" in text
    assert "Recommended starter" in text
    assert "TAVILY_API_KEY" in text
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


def test_register_exposes_core_independent_session_onboarding_surfaces():
    ctx = FakeCtx()

    wsp.register(ctx)

    assert "web-search-plus" not in ctx.cli_commands
    assert "web-search-plus-setup" in ctx.commands
    assert "on_session_start" in ctx.hooks
