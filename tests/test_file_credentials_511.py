"""config.json keys are ignored under a host resolver (Hermes) and never printed (CRED-1, CRED-2)."""

from __future__ import annotations

import json
import logging
import sys

import pytest

from plugin_loader import load_plugin
from wsp_core import config as wsp_config
from wsp_core import credentials

wsp = load_plugin("wsp_plugin_file_credentials_511")

FILE_KEY = "file-secret-key-AAAA1111"
PROFILE_KEY = "profile-secret-key-BBBB2222"


@pytest.fixture(autouse=True)
def _fresh_warnings(monkeypatch):
    monkeypatch.setattr(wsp_config, "_IGNORED_FILE_KEY_WARNED", set(), raising=False)
    for name in ("SERPER_API_KEY", "KEENABLE_API_KEY", "TAVILY_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def _config(**sections):
    return {name: {"api_key": FILE_KEY, **extra} for name, extra in sections.items()}


def _resolver(monkeypatch, values=None):
    values = values or {}
    monkeypatch.setattr(credentials, "_resolver", lambda name, default=None: values.get(name, default))


def test_without_a_resolver_config_json_key_still_works():
    assert credentials.get_secret_resolver() is None
    assert wsp_config.get_api_key("serper", _config(serper={})) == FILE_KEY
    assert wsp_config.provider_configured("serper", _config(serper={})) is True


def test_resolver_installed_ignores_config_json_key(monkeypatch):
    _resolver(monkeypatch)
    config = _config(serper={})
    assert credentials.get_secret_resolver() is not None
    assert wsp_config.get_api_key("serper", config) is None
    assert wsp_config.provider_configured("serper", config) is False


def test_resolver_key_wins_over_config_json_key(monkeypatch):
    _resolver(monkeypatch, {"SERPER_API_KEY": PROFILE_KEY})
    assert wsp_config.get_api_key("serper", _config(serper={})) == PROFILE_KEY


def test_apikey_camelcase_is_ignored_too(monkeypatch):
    _resolver(monkeypatch)
    assert wsp_config.get_api_key("serper", {"serper": {"apiKey": FILE_KEY}}) is None


def test_validate_api_key_fails_without_calling_the_provider(monkeypatch):
    from wsp_core.errors_v3 import MissingProviderKeyError

    _resolver(monkeypatch)
    with pytest.raises(MissingProviderKeyError):
        wsp_config.validate_api_key("serper", _config(serper={}))


def test_check_fn_is_false_when_only_config_json_has_a_key(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_config(serper={}, tavily={})), encoding="utf-8")
    monkeypatch.setenv("WEB_SEARCH_PLUS_CONFIG", str(path))
    _resolver(monkeypatch)

    class Ctx:
        tools = {}

        def register_tool(self, **kwargs):
            self.tools[kwargs["name"]] = kwargs

        def __getattr__(self, name):
            return lambda *a, **k: None

    ctx = Ctx()
    monkeypatch.setattr(wsp, "_install_hermes_secret_resolver", lambda: None)
    wsp.register(ctx)
    assert ctx.tools["web_search_plus"]["check_fn"]() is False
    assert ctx.tools["web_extract_plus"]["check_fn"]() is False

    monkeypatch.setattr(credentials, "_resolver", None)
    assert ctx.tools["web_search_plus"]["check_fn"]() is True


def test_status_marks_file_key_unconfigured_under_resolver_and_configured_without(monkeypatch):
    config = _config(keenable={}, serper={})
    _resolver(monkeypatch)
    status = wsp._provider_config_status(env={}, config=config)
    assert status["providers"]["serper"]["configured"] is False
    assert status["providers"]["keenable"]["configured"] is False
    monkeypatch.setattr(credentials, "_resolver", None)
    status = wsp._provider_config_status(env={}, config=config)
    assert status["providers"]["serper"]["configured"] is True


def _run_cli(argv):
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(argv)
    args.func(args)


def test_status_reports_ignored_key_once_without_the_value(tmp_path, monkeypatch, capsys, caplog):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"version": 1, **_config(serper={}, keenable={})}), encoding="utf-8")
    _resolver(monkeypatch)
    with caplog.at_level(logging.WARNING):
        _run_cli(["status", "--config-path", str(path)])
        _run_cli(["status", "--config-path", str(path)])
        wsp_config.get_api_key("serper", json.loads(path.read_text(encoding="utf-8")))
    out = capsys.readouterr().out
    assert FILE_KEY not in out
    assert out.count("config.json contains an API key for serper; it is ignored") == 2  # one per status run
    assert "hermes config set SERPER_API_KEY" in out
    warnings = [r.getMessage() for r in caplog.records if "config.json contains an API key for serper" in r.getMessage()]
    assert len(warnings) == 1
    assert FILE_KEY not in caplog.text


@pytest.mark.parametrize("with_resolver", [True, False])
def test_status_and_config_json_never_contain_the_key(tmp_path, monkeypatch, capsys, with_resolver):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"version": 1, **_config(serper={}, keenable={"allow_public": True}, searxng={})}), encoding="utf-8")
    if with_resolver:
        _resolver(monkeypatch, {"TAVILY_API_KEY": PROFILE_KEY, "TYPESAFE_API_KEY": PROFILE_KEY})
    for argv in (["status", "--json"], ["config", "show", "--json"]):
        _run_cli([*argv, "--config-path", str(path)])
        out = capsys.readouterr().out
        assert FILE_KEY not in out
        assert PROFILE_KEY not in out
        json.loads(out)


def test_config_dry_run_redacts_existing_keys_but_still_preserves_them_on_write(tmp_path, capsys):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"version": 1, **_config(keenable={})}), encoding="utf-8")
    _run_cli(["config", "set-fallback", "tavily", "--config-path", str(path), "--dry-run"])
    out = capsys.readouterr().out
    assert FILE_KEY not in out
    assert '"fallback_provider": "tavily"' in out
    _run_cli(["config", "set-fallback", "tavily", "--config-path", str(path)])
    assert json.loads(path.read_text(encoding="utf-8"))["keenable"]["api_key"] == FILE_KEY


def test_redact_secrets_masks_any_secret_field_and_keeps_the_rest():
    data = {
        "serper": {"api_key": FILE_KEY, "apiKey": FILE_KEY, "api_key_file": "/x/key"},
        "env": {"TAVILY_API_KEY": FILE_KEY, "TYPESAFE_API_KEY": "", "max_tokens": 5},
        "list": [{"token": FILE_KEY}],
        "name": "keep",
    }
    redacted = credentials.redact_secrets(data)
    assert FILE_KEY not in json.dumps(redacted)
    assert redacted["serper"]["api_key"] == "set"
    assert redacted["env"]["TYPESAFE_API_KEY"] == "not set"
    assert redacted["serper"]["api_key_file"] == "/x/key"
    assert redacted["env"]["max_tokens"] == 5
    assert redacted["name"] == "keep"
    assert data["serper"]["api_key"] == FILE_KEY  # input untouched


SEARXNG_URL = "https://user:pass-SECRET@search.example.net/path"


def _settings_config():
    return {"version": 1, "keenable": {"allow_public": True}, "searxng": {"base_url": SEARXNG_URL}}


def test_ignored_settings_reported_under_resolver_without_url(tmp_path, monkeypatch, capsys, caplog):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_settings_config()), encoding="utf-8")
    _resolver(monkeypatch)
    with caplog.at_level(logging.WARNING):
        _run_cli(["status", "--config-path", str(path)])
        _run_cli(["status", "--json", "--config-path", str(path)])
    out = capsys.readouterr().out
    assert "keenable.allow_public; it is ignored" in out
    assert "hermes config set KEENABLE_ALLOW_PUBLIC 1" in out
    assert "hermes config set SEARXNG_INSTANCE_URL" in out
    assert "pass-SECRET" not in out
    assert "pass-SECRET" not in caplog.text and "search.example.net" not in caplog.text
    assert len([r for r in caplog.records if "keenable.allow_public" in r.getMessage()]) == 1
    payload = json.loads(out[out.index('{'):])
    assert len(payload["ignored_file_settings"]) == 2
    assert not any("search.example.net" in n or "pass-SECRET" in n for n in payload["ignored_file_settings"])
    assert "ignored_file_keys" in payload


def test_ignored_settings_silent_without_resolver(tmp_path, capsys, monkeypatch):
    # Hermetic on machines with Hermes installed: status would install the
    # real resolver when agent.secret_scope is importable.
    monkeypatch.setitem(sys.modules, "agent.secret_scope", None)
    monkeypatch.setattr(wsp, "_install_hermes_secret_resolver", lambda: None)
    assert credentials.get_secret_resolver() is None
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_settings_config()), encoding="utf-8")
    _run_cli(["status", "--json", "--config-path", str(path)])
    out = capsys.readouterr().out
    assert json.loads(out[out.index('{'):])["ignored_file_settings"] == []
    assert wsp_config.keyless_public_allowed("keenable", _settings_config()) is True


def test_profile_env_still_enables_public_tier_under_resolver(monkeypatch):
    _resolver(monkeypatch)
    assert wsp_config.keyless_public_allowed("keenable", _settings_config()) is False
    _resolver(monkeypatch, {"KEENABLE_ALLOW_PUBLIC": "1"})
    assert wsp_config.keyless_public_allowed("keenable", {}) is True
