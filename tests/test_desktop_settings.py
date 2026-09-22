"""Desktop Capabilities settings land in plugins.entries.web-search-plus.settings.

The form writes ~/.hermes/config.yaml. WSP still owns config.json. Declared
scalars from that settings block overlay config.json. Anything else, including
secret material, is ignored. A config path outside <home>/plugins/config.json
does not consult Hermes config, so sterile tests stay sterile.
"""

from __future__ import annotations

import json
from pathlib import Path

from config import _DESKTOP_SETTING_KEYS, load_config


ROOT = Path(__file__).resolve().parents[1]


def _write_plugin_config(home: Path, body: dict) -> Path:
    plugins = home / "plugins"
    plugins.mkdir(parents=True)
    path = plugins / "config.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def test_declared_desktop_settings_overlay_config_json(tmp_path, monkeypatch):
    path = _write_plugin_config(tmp_path, {
        "version": 1,
        "defaults": {"locale": {"country": "fr", "language": "fr"}, "max_results": 5},
        "auto_routing": {"enabled": True, "provider_priority": ["serper"]},
        "searxng": {"base_url": "https://old.example"},
    })
    (tmp_path / "config.yaml").write_text(
        "\n".join([
            "plugins:",
            "  entries:",
            "    web-search-plus:",
            "      settings:",
            "        country: AT",
            "        language: de",
            "        max_results: 3",
            "        auto_routing: false",
            "        searxng_url: https://search.example",
            "        serper_api_key: should-not-leak",
            "        note: not-a-setting",
            "        profile: self_hosted",
            "        provider_priority: evil",
        ]) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("WEB_SEARCH_PLUS_CONFIG", str(path))

    loaded = load_config()

    assert loaded["defaults"]["locale"]["country"] == "at"
    assert loaded["defaults"]["locale"]["language"] == "de"
    assert loaded["defaults"]["max_results"] == 3
    assert loaded["auto_routing"]["enabled"] is False
    assert loaded["auto_routing"]["provider_priority"][0] == "serper"
    assert "evil" not in loaded["auto_routing"]["provider_priority"]
    assert loaded["searxng"]["base_url"] == "https://search.example"
    assert loaded.get("profile", "standard") == "standard"
    dumped = json.dumps(loaded)
    assert "should-not-leak" not in dumped
    assert "not-a-setting" not in dumped
    assert "evil" not in dumped
    assert "note" not in loaded


def test_omitted_desktop_keys_keep_config_json(tmp_path, monkeypatch):
    path = _write_plugin_config(tmp_path, {
        "version": 1,
        "defaults": {"locale": {"country": "fr", "language": "fr"}, "max_results": 8},
        "auto_routing": {"enabled": True},
    })
    (tmp_path / "config.yaml").write_text(
        "plugins:\n  entries:\n    web-search-plus:\n      settings:\n        country: \"\"\n        max_results: 0\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("WEB_SEARCH_PLUS_CONFIG", str(path))

    loaded = load_config()

    assert loaded["defaults"]["locale"]["country"] == "fr"
    assert loaded["defaults"]["max_results"] == 8
    assert loaded["auto_routing"]["enabled"] is True


def test_config_outside_plugins_dir_ignores_hermes_home(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"version": 1, "defaults": {"locale": {"country": "fr"}}}), encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "plugins:\n  entries:\n    web-search-plus:\n      settings:\n        country: zz\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("WEB_SEARCH_PLUS_CONFIG", str(config_path))
    monkeypatch.setenv("HERMES_HOME", str(home))

    loaded = load_config()

    assert loaded["defaults"]["locale"]["country"] == "fr"


def test_broken_hermes_yaml_does_not_break_load(tmp_path, monkeypatch):
    path = _write_plugin_config(tmp_path, {"version": 1})
    (tmp_path / "config.yaml").write_text("plugins: [\n", encoding="utf-8")
    monkeypatch.setenv("WEB_SEARCH_PLUS_CONFIG", str(path))

    loaded = load_config()

    assert loaded["defaults"]["max_results"] == 5


_DESKTOP_EXCLUDED_ENV = frozenset({"DONSETCH_BIN", "SEARXNG_INSTANCE_URL"})


def _yaml_block(text: str, header: str) -> list[str]:
    lines = text.splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith(header))
    body: list[str] = []
    for line in lines[start + 1:]:
        if line and not line.startswith((" ", "#")):
            break
        body.append(line)
    return body


def _optional_env_names(text: str) -> set[str]:
    return {
        line.split("-", 1)[1].strip()
        for line in _yaml_block(text, "optional_env:")
        if line.strip().startswith("- ")
    }


def _schema_secret_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in _yaml_block(text, "config_schema:"):
        if "type: secret" not in line or "env:" not in line:
            continue
        key = line.split(":", 1)[0].strip()
        env_name = line.split("env:", 1)[1].split(",", 1)[0].strip()
        fields[key] = env_name
    return fields


def test_manifest_declares_desktop_form_fields():
    text = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    secrets = _schema_secret_fields(text)
    assert "config_schema:" in text
    assert "auto_routing:" in text
    assert set(secrets.values()) == _optional_env_names(text) - _DESKTOP_EXCLUDED_ENV
    assert _DESKTOP_EXCLUDED_ENV.isdisjoint(secrets.values())
    assert set(_DESKTOP_SETTING_KEYS).isdisjoint(secrets)
    for key in ("country", "language", "max_results", "auto_routing", "searxng_url"):
        assert key in _DESKTOP_SETTING_KEYS
    schema = "\n".join(_yaml_block(text, "config_schema:"))
    for key in ("serpbase_api_key", "querit_api_key", "monid_api_key", "tinyfish_api_key"):
        assert f"{key}:" in schema
        assert "explicit-only" in schema.split(f"{key}:", 1)[1].split("\n", 1)[0]
    for key in ("serper_api_key", "exa_api_key", "firecrawl_api_key", "parallel_api_key"):
        assert "explicit-only" not in schema.split(f"{key}:", 1)[1].split("\n", 1)[0]


def test_secret_settings_never_overlay(tmp_path, monkeypatch):
    text = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    secrets = _schema_secret_fields(text)
    path = _write_plugin_config(tmp_path, {"version": 1, "defaults": {"max_results": 5}})
    settings = ["plugins:", "  entries:", "    web-search-plus:", "      settings:"]
    poisons = {key: f"leak-{key}" for key in secrets}
    settings.extend(f"        {key}: {poison}" for key, poison in poisons.items())
    (tmp_path / "config.yaml").write_text("\n".join(settings) + "\n", encoding="utf-8")
    monkeypatch.setenv("WEB_SEARCH_PLUS_CONFIG", str(path))

    dumped = json.dumps(load_config())

    for poison in poisons.values():
        assert poison not in dumped
