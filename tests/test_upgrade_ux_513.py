"""5.1.3: what a normal user sees after upgrading from 5.0 and with Hermes' own web_search.

Hermetic: no Hermes install needed (agent.secret_scope is blocked), the plugin
directory, HERMES_HOME and the Hermes config.yaml live in tmp_path.
"""

from __future__ import annotations

import builtins
import json
import logging
import os
import sys

import pytest

from plugin_loader import load_plugin
from wsp_core import credentials

wsp = load_plugin("wsp_plugin_upgrade_ux_513")

PROVIDER_VARS = [name for name in os.environ if name.endswith("_API_KEY")] + [
    "BRAVE_API_KEY", "SERPER_API_KEY", "TAVILY_API_KEY", "EXA_API_KEY", "KEENABLE_API_KEY",
    "LINKUP_API_KEY", "SEARXNG_INSTANCE_URL", "KEENABLE_ALLOW_PUBLIC",
]
KEY = "brave-secret-key-CCCC3333"


class Ctx:
    def __init__(self):
        self.tools = {}

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs

    def __getattr__(self, name):
        return lambda *a, **k: None


@pytest.fixture
def hermes(tmp_path, monkeypatch):
    """A Hermes install under tmp_path: plugin dir, profile without keys, empty config.yaml."""
    for name in PROVIDER_VARS:
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    plugin_dir = home / ".hermes" / "plugins" / "web-search-plus"
    plugin_dir.mkdir(parents=True)
    config_yaml = home / ".hermes" / "config.yaml"
    config_yaml.write_text("model: x\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes"))
    monkeypatch.setenv("HERMES_CONFIG", str(config_yaml))
    monkeypatch.setenv("WEB_SEARCH_PLUS_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setitem(sys.modules, "agent", None)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", None)
    monkeypatch.setattr(wsp, "_install_hermes_secret_resolver", lambda: None)
    monkeypatch.setattr(wsp, "_plugin_dir", lambda: plugin_dir)
    profile = {}
    monkeypatch.setattr(credentials, "_resolver", lambda name, default=None: profile.get(name, default))
    wsp._test_profile = profile
    wsp._test_config_yaml = config_yaml
    wsp._test_plugin_dir = plugin_dir
    return profile


def _register():
    ctx = Ctx()
    wsp.register(ctx)
    return ctx.tools


def _run_cli(argv):
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(argv)
    args.func(args)


def _status(capsys, *extra):
    _run_cli(["status", *extra])
    return capsys.readouterr().out


def _forbid_reading(monkeypatch, forbidden):
    real_open = builtins.open
    forbidden = str(forbidden)

    def guarded(file, *args, **kwargs):
        if str(file) == forbidden:
            raise AssertionError(f"{forbidden} must not be opened")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded)
    monkeypatch.setattr(wsp.Path, "read_text", _guard_path(wsp.Path.read_text, forbidden))
    monkeypatch.setattr(wsp.Path, "read_bytes", _guard_path(wsp.Path.read_bytes, forbidden))


def _guard_path(real, forbidden):
    def guarded(self, *args, **kwargs):
        if str(self) == forbidden:
            raise AssertionError(f"{forbidden} must not be read")
        return real(self, *args, **kwargs)

    return guarded


@pytest.mark.parametrize("where", ["plugin_dir", "plugins_parent"])
def test_upgrader_is_told_where_the_old_env_is_and_what_to_run(hermes, monkeypatch, capsys, where, caplog):
    folder = wsp._test_plugin_dir if where == "plugin_dir" else wsp._test_plugin_dir.parent
    env_file = folder / ".env"
    env_file.write_text(f"BRAVE_API_KEY={KEY}\n", encoding="utf-8")
    env_file.chmod(0)
    _forbid_reading(monkeypatch, env_file)

    with caplog.at_level(logging.WARNING):
        tools = _register()
    out = _status(capsys)
    shown = str(folder).replace(str(os.environ["HOME"]), "~")
    assert f"! A .env file in {shown} is no longer read since 5.1." in out
    assert "hermes config set BRAVE_API_KEY <key>" in out
    assert "then /reload" in out
    assert KEY not in out and KEY not in caplog.text
    assert [r for r in caplog.records if "no longer read since 5.1" in r.getMessage()]

    data = json.loads(_status(capsys, "--json"))
    assert data["legacy_env_files"] == [str(env_file)]
    assert "providers" in data and "hermes_native" in data

    assert tools["web_search_plus"]["check_fn"]() is True
    assert tools["web_extract_plus"]["check_fn"]() is True
    for name, call in (("web_search_plus", {"query": "hello"}), ("web_extract_plus", {"urls": ["https://example.com"]})):
        message = tools[name]["handler"](call)
        assert "not read since Web Search Plus 5.1" in message
        assert "`hermes config set BRAVE_API_KEY <key>`" in message
        assert "then run /reload" in message
        assert KEY not in message


def test_upgrader_setup_shows_the_same_notice(hermes, capsys):
    (wsp._test_plugin_dir / ".env").write_text(f"BRAVE_API_KEY={KEY}\n", encoding="utf-8")
    _run_cli(["setup", "--dry-run"])
    out = capsys.readouterr().out
    assert "is no longer read since 5.1" in out
    assert KEY not in out


def test_once_the_key_is_in_the_profile_the_old_env_no_longer_blocks_calls(hermes, monkeypatch):
    (wsp._test_plugin_dir / ".env").write_text(f"BRAVE_API_KEY={KEY}\n", encoding="utf-8")
    hermes["BRAVE_API_KEY"] = "profile-key"
    monkeypatch.setattr(wsp, "_run_search", lambda **kwargs: {"results": [], "provider": "brave", "query": kwargs["query"]})
    tools = _register()
    assert tools["web_search_plus"]["check_fn"]() is True
    assert "not read since Web Search Plus 5.1" not in tools["web_search_plus"]["handler"]({"query": "hello"})


def test_fresh_user_without_keys_and_without_old_env_is_unchanged(hermes, capsys, caplog):
    with caplog.at_level(logging.WARNING):
        tools = _register()
    assert tools["web_search_plus"]["check_fn"]() is False
    assert tools["web_extract_plus"]["check_fn"]() is False
    out = _status(capsys)
    assert "no longer read" not in out
    assert "Hermes' own web_search" not in out
    assert "0/" in out or "no provider keys" in out
    assert json.loads(_status(capsys, "--json"))["legacy_env_files"] == []
    assert not [r for r in caplog.records if "no longer read" in r.getMessage()]


def test_brave_user_is_told_how_to_route_hermes_search_through_wsp(hermes, capsys):
    hermes["BRAVE_API_KEY"] = KEY
    out = _status(capsys)
    assert "Hermes' own web_search does not use your Web Search Plus keys." in out
    assert "`hermes config set web.search_backend wsp`" in out
    # Brave cannot extract: recommending a wsp extract backend would fail every call.
    assert "web.extract_backend wsp" not in out
    # Without an extract pin Hermes' autodetect would send web_extract to wsp too.
    assert "`hermes config set web.extract_backend exa`" in out
    assert "tools.tool_search.never_defer" in out
    assert KEY not in out
    data = json.loads(_status(capsys, "--json"))
    assert "web.search_backend wsp" in data["native_backend_hint"]


def test_search_and_extract_keys_get_both_backend_commands(hermes, capsys):
    hermes["BRAVE_API_KEY"] = KEY
    hermes["TAVILY_API_KEY"] = KEY
    out = _status(capsys)
    assert "`hermes config set web.search_backend wsp`" in out
    assert "`hermes config set web.extract_backend wsp`" in out
    assert "web.extract_backend exa" not in out


def test_wsp_extract_backend_without_an_extraction_key_is_flagged(hermes, capsys):
    # Seen live: Brave only + extract_backend wsp -> every extract failed in WSP and
    # Hermes' keyless rescue sent the URL to anonymous services, silently.
    hermes["BRAVE_API_KEY"] = KEY
    wsp._test_config_yaml.write_text("web:\n  search_backend: wsp\n  extract_backend: wsp\n", encoding="utf-8")
    out = _status(capsys)
    assert "none of your Web Search Plus keys can extract pages" in out
    assert "keyless rescue" in out
    assert "`hermes config set web.extract_backend exa`" in out
    assert KEY not in out


def test_brave_user_with_wsp_backend_gets_no_hint(hermes, capsys):
    hermes["BRAVE_API_KEY"] = KEY
    hermes["TAVILY_API_KEY"] = KEY
    wsp._test_config_yaml.write_text("web:\n  search_backend: wsp\n  extract_backend: wsp\n", encoding="utf-8")
    out = _status(capsys)
    assert "does not use your Web Search Plus keys" not in out
    assert json.loads(_status(capsys, "--json"))["native_backend_hint"] is None


def test_search_on_wsp_but_extract_not_gets_only_the_extract_hint(hermes, capsys):
    hermes["BRAVE_API_KEY"] = KEY
    hermes["LINKUP_API_KEY"] = KEY
    wsp._test_config_yaml.write_text("web:\n  search_backend: wsp\n", encoding="utf-8")
    hint = json.loads(_status(capsys, "--json"))["native_backend_hint"]
    assert "web_extract" in hint and "hermes config set web.extract_backend wsp" in hint
    assert "web.search_backend" not in hint


def test_brave_user_setup_shows_the_hint(hermes, capsys):
    hermes["BRAVE_API_KEY"] = KEY
    _run_cli(["setup", "--dry-run"])
    assert "hermes config set web.search_backend wsp" in capsys.readouterr().out


def test_mcp_standalone_still_loads_env_and_shows_no_legacy_hint(tmp_path, monkeypatch, capsys):
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / ".env").write_text(f"BRAVE_API_KEY={KEY}\n", encoding="utf-8")
    monkeypatch.setattr(wsp, "_plugin_dir", lambda: plugin_dir)
    monkeypatch.setattr(credentials, "_resolver", None)
    assert wsp._legacy_env_files() == []
    assert wsp._legacy_env_lines() == []
    from wsp_core import env_loader

    target = {}
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "nohome"))
    env_loader.load_env_files(plugin_dir / "anchor.py", environ=target)
    assert target["BRAVE_API_KEY"] == KEY


def test_descriptions_no_longer_steer_the_model_away_from_wsp(hermes):
    tools = _register()
    for name in ("web_search_plus", "web_extract_plus"):
        description = tools[name]["schema"]["description"]
        assert "instead of web_search" not in description
        assert "web_search." not in description.replace("web_search_plus", "")


def test_old_env_notice_changes_once_the_profile_has_keys(hermes, capsys):
    (wsp._test_plugin_dir / ".env").write_text(f"BRAVE_API_KEY={KEY}\n", encoding="utf-8")
    assert "Move each key into your Hermes profile" in _status(capsys)
    hermes["BRAVE_API_KEY"] = "profile-key"
    out = _status(capsys)
    assert "Move each key into your Hermes profile" not in out
    assert "the file is not needed; you can delete it" in out


def test_extract_migration_message_says_nothing_was_extracted(hermes):
    (wsp._test_plugin_dir / ".env").write_text(f"BRAVE_API_KEY={KEY}\n", encoding="utf-8")
    tools = _register()
    message = tools["web_extract_plus"]["handler"]({"urls": ["https://example.org"]})
    assert "nothing was extracted" in message and "nothing was searched" not in message


def test_search_on_wsp_without_extract_pin_and_without_extraction_key_is_flagged(hermes, capsys):
    # Seen live: following "search_backend wsp" alone, Hermes picked wsp for web_extract
    # too ("search-only backend and cannot extract URL content") and extraction broke.
    hermes["BRAVE_API_KEY"] = KEY
    wsp._test_config_yaml.write_text("web:\n  search_backend: wsp\n", encoding="utf-8")
    hint = json.loads(_status(capsys, "--json"))["native_backend_hint"]
    assert "Hermes picks wsp for web_extract" in hint
    assert "`hermes config set web.extract_backend exa`" in hint


def test_search_on_wsp_with_a_hermes_extract_backend_is_fine(hermes, capsys):
    hermes["BRAVE_API_KEY"] = KEY
    wsp._test_config_yaml.write_text("web:\n  search_backend: wsp\n  extract_backend: exa\n", encoding="utf-8")
    assert json.loads(_status(capsys, "--json"))["native_backend_hint"] is None
