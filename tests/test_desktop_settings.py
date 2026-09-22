"""Desktop Capabilities settings land in plugins.entries.web-search-plus.settings.

The form writes ~/.hermes/config.yaml. WSP still owns config.json. Declared
scalars from that settings block overlay config.json. Anything else, including
secret material, is ignored. A config path outside <home>/plugins/config.json
does not consult Hermes config, so sterile tests stay sterile.
"""

from __future__ import annotations

import builtins
import json
from pathlib import Path

from config import load_config


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
    assert loaded["searxng"]["base_url"] == "https://search.example"
    dumped = json.dumps(loaded)
    assert "should-not-leak" not in dumped
    assert "not-a-setting" not in dumped
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


def test_manifest_declares_desktop_form_fields():
    text = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    assert "config_schema:" in text
    assert "type: secret" in text
    assert "env: SERPER_API_KEY" in text
    assert "auto_routing:" in text
    for env_name in (
        "SERPER_API_KEY",
        "BRAVE_API_KEY",
        "TAVILY_API_KEY",
        "YOU_API_KEY",
        "LINKUP_API_KEY",
    ):
        assert f"env: {env_name}" in text
    assert "env: DONSETCH_BIN" not in text
    assert "env: SEARXNG_INSTANCE_URL" not in text


def test_desktop_overlay_without_pyyaml(tmp_path, monkeypatch):
    path = _write_plugin_config(tmp_path, {
        "version": 1,
        "defaults": {"locale": {"country": "fr"}, "max_results": 5},
    })
    (tmp_path / "config.yaml").write_text(
        "\n".join([
            "plugins:",
            "  entries:",
            "    web-search-plus:",
            "      settings:",
            "        country: AT",
        ]) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("WEB_SEARCH_PLUS_CONFIG", str(path))
    real_import = builtins.__import__

    def _block_yaml(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "yaml":
            raise ImportError("PyYAML absent")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _block_yaml)

    loaded = load_config()

    assert loaded["defaults"]["locale"]["country"] == "at"
