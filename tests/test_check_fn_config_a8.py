"""A8: tool check_fn and the slash setup command must see config.json and Desktop keys.

Hermes hides a tool when check_fn returns False. Keys that live only in
config.json, or a SearXNG URL from config.json or the Desktop settings, must
keep the tools visible. No test here touches the network: DNS for the public
SearXNG host is mocked.
"""

from __future__ import annotations

import json
import socket

import pytest

from plugin_loader import load_plugin

wsp = load_plugin("wsp_plugin_check_fn_config_a8")

PUBLIC_HOST = "searx.example.net"
PUBLIC_IP = "93.184.216.34"


class FakeCtx:
    def __init__(self):
        self.tools = {}
        self.commands = {}

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name] = handler


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    """Point the engine at an isolated config.json with no env keys set."""
    path = tmp_path / "config.json"
    monkeypatch.setenv("WEB_SEARCH_PLUS_CONFIG", str(path))
    for key in (*wsp._PROVIDER_ENV_KEYS, "SEARXNG_INSTANCE_URL", "SEARXNG_ALLOW_PRIVATE"):
        monkeypatch.delenv(key, raising=False)
    return path


@pytest.fixture
def public_dns(monkeypatch):
    """Resolve PUBLIC_HOST to a public address; every other host keeps the old resolver."""
    real_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(host, port, *args, **kwargs):
        if host == PUBLIC_HOST:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, port or 0))]
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


def _register():
    ctx = FakeCtx()
    wsp.register(ctx)
    return ctx


def _check(ctx, name):
    return ctx.tools[name]["check_fn"]()


def test_config_json_serper_key_enables_search_and_extract(config_path):
    config_path.write_text(json.dumps({"serper": {"api_key": "serper-config-test"}}), encoding="utf-8")

    ctx = _register()

    assert _check(ctx, "web_search_plus") is True
    assert _check(ctx, "web_extract_plus") is True


def test_config_json_searxng_base_url_enables_search_only(config_path, public_dns):
    config_path.write_text(
        json.dumps({"searxng": {"base_url": f"https://{PUBLIC_HOST}"}}), encoding="utf-8"
    )

    ctx = _register()

    assert _check(ctx, "web_search_plus") is True
    assert _check(ctx, "web_extract_plus") is False


def test_desktop_searxng_url_enables_search(tmp_path, monkeypatch, public_dns):
    # Hermes Desktop settings are read only for <home>/plugins/config.json and <home>/config.yaml.
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    config_path = plugins_dir / "config.json"
    monkeypatch.setenv("WEB_SEARCH_PLUS_CONFIG", str(config_path))
    monkeypatch.delenv("SEARXNG_INSTANCE_URL", raising=False)
    (tmp_path / "config.yaml").write_text(
        "\n".join([
            "plugins:",
            "  entries:",
            "    web-search-plus:",
            "      settings:",
            f"        searxng_url: https://{PUBLIC_HOST}",
        ]) + "\n",
        encoding="utf-8",
    )

    ctx = _register()

    assert _check(ctx, "web_search_plus") is True
    assert _check(ctx, "web_extract_plus") is False


def test_nothing_configured_returns_false(config_path):
    config_path.write_text(
        json.dumps({"serper": {"api_key": ""}, "searxng": {"base_url": ""}}), encoding="utf-8"
    )

    ctx = _register()

    assert _check(ctx, "web_search_plus") is False
    assert _check(ctx, "web_extract_plus") is False


def test_private_searxng_url_is_not_configured_and_does_not_raise(config_path):
    # Loopback is refused by the engine's SSRF guard; check_fn must treat it as unconfigured.
    config_path.write_text(
        json.dumps({"searxng": {"base_url": "http://127.0.0.1:8888"}}), encoding="utf-8"
    )

    ctx = _register()

    assert _check(ctx, "web_search_plus") is False
    assert _check(ctx, "web_extract_plus") is False


@pytest.mark.parametrize("content", ["{not json", "[1, 2]"])
def test_broken_config_file_never_raises_from_check_fn(config_path, content):
    config_path.write_text(content, encoding="utf-8")

    ctx = _register()

    assert _check(ctx, "web_search_plus") is False
    assert _check(ctx, "web_extract_plus") is False


def test_broken_config_falls_back_to_env_keys(config_path, monkeypatch):
    config_path.write_text("[1, 2]", encoding="utf-8")
    monkeypatch.setenv("BRAVE_API_KEY", "brave-env-test")

    ctx = _register()

    assert _check(ctx, "web_search_plus") is True
    assert _check(ctx, "web_extract_plus") is False


def test_load_config_raising_from_check_fn_is_contained(config_path, monkeypatch):
    def boom():
        raise RuntimeError("config loader exploded")

    monkeypatch.setattr(wsp, "load_config", boom)

    ctx = _register()

    assert _check(ctx, "web_search_plus") is False
    assert _check(ctx, "web_extract_plus") is False


def test_slash_setup_reports_keys_from_config_json(config_path):
    config_path.write_text(json.dumps({"serper": {"api_key": "serper-config-test"}}), encoding="utf-8")
    ctx = _register()

    output = ctx.commands["web-search-plus-setup"]()

    assert "no provider keys are configured" not in output
    assert "configured. Providers:" in output
    assert "search=yes" in output


def test_slash_setup_survives_broken_config(config_path):
    config_path.write_text("[1, 2]", encoding="utf-8")
    ctx = _register()

    output = ctx.commands["web-search-plus-setup"]()

    assert "no provider keys are configured" in output
