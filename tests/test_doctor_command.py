from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


SEARCH_PATH = Path(__file__).resolve().parents[1] / "search.py"
search_spec = importlib.util.spec_from_file_location("wsp_search_doctor_under_test", SEARCH_PATH)
search = importlib.util.module_from_spec(search_spec)
assert search_spec.loader is not None
search_spec.loader.exec_module(search)


PROVIDER_ENV_VARS = [
    "SERPER_API_KEY",
    "SERPBASE_API_KEY",
    "BRAVE_API_KEY",
    "TAVILY_API_KEY",
    "QUERIT_API_KEY",
    "LINKUP_API_KEY",
    "EXA_API_KEY",
    "YOU_API_KEY",
    "PARALLEL_API_KEY",
    "PERPLEXITY_API_KEY",
    "KILOCODE_API_KEY",
    "FIRECRAWL_API_KEY",
    "SEARXNG_INSTANCE_URL",
]


def _clear_provider_env(monkeypatch):
    for name in PROVIDER_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_doctor_json_reports_provider_capabilities_without_secrets(monkeypatch, tmp_path, capsys):
    _clear_provider_env(monkeypatch)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"version": 1, "auto_routing": {"disabled_providers": ["brave"]}}))
    monkeypatch.setenv("WEB_SEARCH_PLUS_CONFIG", str(config_path))
    monkeypatch.setenv("SERPER_API_KEY", "very-sensitive-serper-secret")
    monkeypatch.setenv("TAVILY_API_KEY", "very-sensitive-tavily-secret")
    monkeypatch.setattr(search, "provider_in_cooldown", lambda _provider: (False, 0))
    monkeypatch.setattr(sys, "argv", ["search.py", "doctor", "--json"])

    search.main()

    stdout = capsys.readouterr().out
    data = json.loads(stdout)
    assert data["ok"] is True
    assert data["mode"] == "offline"
    assert "very-sensitive" not in stdout

    providers = {provider["provider"]: provider for provider in data["providers"]}
    assert providers["serper"] == {
        "provider": "serper",
        "env_var": "SERPER_API_KEY",
        "search_capable": True,
        "extract_capable": False,
        "key_present": True,
        "auto_allowed": True,
        "disabled": False,
        "cooldown": {"active": False, "remaining_seconds": 0},
    }
    assert providers["tavily"]["search_capable"] is True
    assert providers["tavily"]["extract_capable"] is True
    assert providers["tavily"]["key_present"] is True
    assert providers["brave"]["disabled"] is True


def test_doctor_json_reports_provider_cooldowns(monkeypatch, tmp_path, capsys):
    _clear_provider_env(monkeypatch)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"version": 1}))
    monkeypatch.setenv("WEB_SEARCH_PLUS_CONFIG", str(config_path))
    monkeypatch.setenv("SERPER_API_KEY", "very-sensitive-serper-secret")
    monkeypatch.setattr(search, "provider_in_cooldown", lambda provider: (provider == "serper", 42 if provider == "serper" else 0))
    monkeypatch.setattr(sys, "argv", ["search.py", "doctor", "--json"])

    search.main()

    data = json.loads(capsys.readouterr().out)
    providers = {provider["provider"]: provider for provider in data["providers"]}
    assert providers["serper"]["cooldown"] == {"active": True, "remaining_seconds": 42}


def test_doctor_plain_text_is_human_readable(monkeypatch, tmp_path, capsys):
    _clear_provider_env(monkeypatch)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"version": 1}))
    monkeypatch.setenv("WEB_SEARCH_PLUS_CONFIG", str(config_path))
    monkeypatch.setenv("SERPER_API_KEY", "very-sensitive-serper-secret")
    monkeypatch.setattr(search, "provider_in_cooldown", lambda _provider: (False, 0))
    monkeypatch.setattr(sys, "argv", ["search.py", "doctor"])

    search.main()

    stdout = capsys.readouterr().out
    assert "Web Search Plus Doctor" in stdout
    assert "serper" in stdout
    assert "SERPER_API_KEY" in stdout
    assert "very-sensitive" not in stdout


def test_doctor_reports_provider_config_errors_without_crashing(monkeypatch, tmp_path, capsys):
    _clear_provider_env(monkeypatch)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"version": 1, "searxng": {"instance_url": "http://127.0.0.1:8888"}}))
    monkeypatch.setenv("WEB_SEARCH_PLUS_CONFIG", str(config_path))
    monkeypatch.delenv("SEARXNG_ALLOW_PRIVATE", raising=False)
    monkeypatch.setattr(search, "provider_in_cooldown", lambda _provider: (False, 0))
    monkeypatch.setattr(sys, "argv", ["search.py", "doctor", "--json"])

    search.main()

    data = json.loads(capsys.readouterr().out)
    providers = {provider["provider"]: provider for provider in data["providers"]}
    assert data["ok"] is False
    assert providers["searxng"]["key_present"] is False
    assert providers["searxng"]["error"]["type"] == "config"
    assert "127.0.0.1" not in providers["searxng"]["error"]["message"]


def test_doctor_reports_cooldown_errors_without_crashing(monkeypatch, tmp_path, capsys):
    _clear_provider_env(monkeypatch)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"version": 1}))
    monkeypatch.setenv("WEB_SEARCH_PLUS_CONFIG", str(config_path))
    monkeypatch.setenv("SERPER_API_KEY", "very-sensitive-serper-secret")

    def broken_cooldown(provider):
        if provider == "serper":
            raise ValueError("invalid literal for int() with base 10: 'not-int'")
        return False, 0

    monkeypatch.setattr(search, "provider_in_cooldown", broken_cooldown)
    monkeypatch.setattr(sys, "argv", ["search.py", "doctor", "--json"])

    search.main()

    stdout = capsys.readouterr().out
    data = json.loads(stdout)
    providers = {provider["provider"]: provider for provider in data["providers"]}
    assert data["ok"] is True
    assert providers["serper"]["key_present"] is True
    assert providers["serper"]["cooldown"] == {"active": False, "remaining_seconds": 0}
    assert providers["serper"]["error"]["type"] == "cooldown"
    assert "not-int" not in stdout
