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

import pytest

from config import _DESKTOP_SETTING_KEYS, load_config


ROOT = Path(__file__).resolve().parents[1]


def _write_plugin_config(home: Path, body: dict) -> Path:
    plugins = home / "plugins"
    plugins.mkdir(parents=True, exist_ok=True)
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


def test_flow_style_settings_overlay_without_pyyaml(tmp_path, monkeypatch):
    path = _write_plugin_config(tmp_path, {
        "version": 1,
        "defaults": {"locale": {"country": "fr", "language": "fr"}, "max_results": 5},
        "auto_routing": {"enabled": True, "provider_priority": ["serper"]},
        "searxng": {"base_url": "https://old.example"},
    })
    (tmp_path / "config.yaml").write_text(
        "\n".join([
            "model:",
            "  default: grok",
            "plugins:",
            "  enabled:",
            "    - memory",
            "  entries:",
            "    web-search-plus:",
            "      settings: {country: AT, language: de, max_results: 3, auto_routing: false, searxng_url: https://search.example, serper_api_key: should-not-leak, provider_priority: evil}",
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
    assert loaded["defaults"]["locale"]["language"] == "de"
    assert loaded["defaults"]["max_results"] == 3
    assert loaded["auto_routing"]["enabled"] is False
    assert loaded["auto_routing"]["provider_priority"][0] == "serper"
    assert loaded["searxng"]["base_url"] == "https://search.example"
    dumped = json.dumps(loaded)
    assert "should-not-leak" not in dumped
    assert "evil" not in dumped


def test_nested_flow_settings_are_ignored_without_pyyaml(tmp_path, monkeypatch):
    path = _write_plugin_config(tmp_path, {
        "version": 1,
        "defaults": {"locale": {"country": "fr"}, "max_results": 5},
    })
    (tmp_path / "config.yaml").write_text(
        "plugins:\n  entries:\n    web-search-plus:\n      settings: {country: AT, nested: {a: 1}}\n",
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

    assert loaded["defaults"]["locale"]["country"] == "fr"
    assert loaded["defaults"]["max_results"] == 5


def test_desktop_overlay_without_pyyaml(tmp_path, monkeypatch):
    path = _write_plugin_config(tmp_path, {
        "version": 1,
        "defaults": {"locale": {"country": "fr"}, "max_results": 5},
    })
    (tmp_path / "config.yaml").write_text(
        "\n".join([
            "model:",
            "  default: grok",
            "plugins:",
            "  enabled:",
            "    - memory",
            "  entries:",
            "    web-search-plus:",
            "      settings:",
            "        country: AT",
            "        auto_routing: false",
            "        max_results: 0",
            "        serper_api_key: should-not-leak",
            "        profile: self_hosted",
            "        note: not-a-setting",
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
    assert loaded["defaults"]["max_results"] == 5
    assert loaded["auto_routing"]["enabled"] is False
    assert loaded.get("profile", "standard") == "standard"
    dumped = json.dumps(loaded)
    assert "should-not-leak" not in dumped
    assert "not-a-setting" not in dumped


def _load_desktop_yaml(tmp_path, monkeypatch, text: str, *, block_yaml: bool):
    path = _write_plugin_config(tmp_path, {
        "version": 1,
        "defaults": {"locale": {"country": "fr", "language": "fr"}, "max_results": 5},
        "auto_routing": {"enabled": True, "provider_priority": ["serper"]},
        "searxng": {"base_url": "https://old.example"},
    })
    (tmp_path / "config.yaml").write_text(text, encoding="utf-8")
    monkeypatch.setenv("WEB_SEARCH_PLUS_CONFIG", str(path))
    if block_yaml:
        real_import = builtins.__import__

        def _block(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "yaml":
                raise ImportError("PyYAML absent")
            return real_import(name, globals, locals, fromlist, level)

        monkeypatch.setattr(builtins, "__import__", _block)
    return load_config()


_AGREEMENT_CASES = (
    (
        "flat flow",
        "plugins:\n  entries:\n    web-search-plus:\n      settings: {country: AT, language: de, max_results: 3, auto_routing: false, searxng_url: https://search.example, serper_api_key: leak}\n",
        {"country": "at", "language": "de", "max_results": 3, "routing": False, "url": "https://search.example"},
    ),
    (
        "quoted hash and comma",
        'plugins:\n  entries:\n    web-search-plus:\n      settings: {country: "AT", searxng_url: "https://x.example/a#b,c"}\n',
        {"country": "at", "language": "fr", "max_results": 5, "routing": True, "url": "https://x.example/a#b,c"},
    ),
    (
        "null and tilde do not wipe",
        "plugins:\n  entries:\n    web-search-plus:\n      settings: {country: null, language: ~, max_results: 0, auto_routing: null}\n",
        {"country": "fr", "language": "fr", "max_results": 5, "routing": True, "url": "https://old.example"},
    ),
    (
        "yaml 1.1 bool and trailing comma",
        "plugins:\n  entries:\n    web-search-plus:\n      settings: {country: AT, auto_routing: yes, max_results: 4,}\n",
        {"country": "at", "language": "fr", "max_results": 4, "routing": True, "url": "https://old.example"},
    ),
    (
        "quoted null stays text, empty country does not wipe",
        'plugins:\n  entries:\n    web-search-plus:\n      settings: {country: "", language: "null"}\n',
        {"country": "fr", "language": "null", "max_results": 5, "routing": True, "url": "https://old.example"},
    ),
    (
        "broken flow applies nothing",
        "plugins:\n  entries:\n    web-search-plus:\n      settings: {country: AT\n",
        {"country": "fr", "language": "fr", "max_results": 5, "routing": True, "url": "https://old.example"},
    ),
)


def test_pyyaml_and_fallback_apply_the_same_overlay(tmp_path, monkeypatch):
    real_import = builtins.__import__
    for name, text, expect in _AGREEMENT_CASES:
        for block_yaml in (False, True):
            loaded = _load_desktop_yaml(tmp_path, monkeypatch, text, block_yaml=block_yaml)
            label = f"{name} fallback={block_yaml}"
            assert loaded["defaults"]["locale"]["country"] == expect["country"], label
            assert loaded["defaults"]["locale"]["language"] == expect["language"], label
            assert loaded["defaults"]["max_results"] == expect["max_results"], label
            assert loaded["auto_routing"]["enabled"] is expect["routing"], label
            assert loaded["searxng"]["base_url"] == expect["url"], label
            assert "leak" not in json.dumps(loaded), label
            monkeypatch.setattr(builtins, "__import__", real_import)


def _registered_search_handler():
    import __init__ as plugin

    registered = {}

    class Ctx:
        def register_tool(self, **kwargs):
            registered[kwargs["name"]] = kwargs["handler"]

    plugin.register(Ctx())
    return plugin, registered["web_search_plus"]


def _handler_count(monkeypatch, args, max_results):
    from unittest import mock

    plugin, handler = _registered_search_handler()
    seen = {}

    def fake_run_search(**kwargs):
        seen.update(kwargs)
        return {"provider": "serper", "query": kwargs["query"], "results": []}

    monkeypatch.setattr(plugin, "load_config", lambda: {"defaults": {"max_results": max_results}})
    with mock.patch.object(plugin, "_run_search", fake_run_search):
        handler(args)
    return seen["count"]


def test_tool_uses_configured_max_results_when_count_is_omitted(monkeypatch):
    assert _handler_count(monkeypatch, {"query": "q"}, 8) == 8


def test_explicit_tool_count_beats_configured_max_results(monkeypatch):
    assert _handler_count(monkeypatch, {"query": "q", "count": 3}, 8) == 3


@pytest.mark.parametrize("bad", [None, 0, -2, "x", True, 999])
def test_bad_configured_max_results_falls_back_safely(monkeypatch, bad):
    count = _handler_count(monkeypatch, {"query": "q"}, bad)
    assert count == (20 if bad == 999 else 5)
