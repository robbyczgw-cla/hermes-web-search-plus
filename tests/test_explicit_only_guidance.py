"""5.1.4: a configured explicit-only provider (e.g. DonSeTch) is named, not reported as missing.

Seen live: with DonSeTch as the only provider, an automatic search answered
"No configured search provider is available … Missing API key for serper", and
the model told the user DonSeTch was not set up.
"""
import stat

import pytest

from wsp_core import config as wsp_config


@pytest.fixture
def donsetch_bin(tmp_path):
    """An executable stand-in: since 5.1.8 a missing binary is invalid_value, not configured."""
    path = tmp_path / "donsetch"
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def _guidance(capability, candidates, config=None, requested="auto"):
    payload = {"error": "All providers failed"}
    wsp_config.add_provider_setup_guidance(payload, capability, candidates, config or {}, requested_provider=requested)
    return payload


def test_configured_donsetch_is_named_with_both_ways_to_use_it(monkeypatch, donsetch_bin):
    monkeypatch.setenv("DONSETCH_BIN", donsetch_bin)
    payload = _guidance("search", ["serper", "brave", "tavily"])
    assert payload["error_type"] == "explicit_only_provider_configured"
    assert "DonSeTch (local MCP) is set up but only runs when asked for" in payload["error"]
    assert 'provider="donsetch"' in payload["error"]
    assert "hermes web-search-plus config set-auto-allow donsetch on" in payload["error"]
    assert payload["explicit_only_providers"] == ["donsetch"]
    assert "Missing API key" not in payload["error"]


def test_extract_wording(monkeypatch, donsetch_bin):
    monkeypatch.setenv("DONSETCH_BIN", donsetch_bin)
    payload = _guidance("extract", ["tavily", "exa"])
    assert payload["error"].startswith("No provider for automatic extraction is configured.")


def test_nothing_configured_keeps_the_setup_hint():
    payload = _guidance("search", ["serper", "brave", "tavily"])
    assert payload["error_type"] == "provider_setup_required"
    assert "hermes web-search-plus setup --preset starter" in payload["error"]


def test_auto_allowed_donsetch_is_not_reported_as_explicit_only(monkeypatch, donsetch_bin):
    monkeypatch.setenv("DONSETCH_BIN", donsetch_bin)
    config = {"auto_routing": {"auto_allow": {"donsetch": True}}}
    payload = _guidance("search", ["serper", "brave"], config)
    assert payload.get("error_type") != "explicit_only_provider_configured"


def test_explicit_request_for_an_unconfigured_provider_is_unchanged(monkeypatch, donsetch_bin):
    monkeypatch.setenv("DONSETCH_BIN", donsetch_bin)
    payload = _guidance("search", ["serper"], requested="serper")
    assert payload["error_type"] == "requested_provider_not_configured"


def test_set_auto_allow_alone_puts_donsetch_into_auto_search(monkeypatch, tmp_path, donsetch_bin):
    # The hint names one command. Before, set-auto-allow only wrote the flag:
    # auto routing tries provider_priority alone, so DonSeTch stayed out and the
    # search went to the serper filler.
    import argparse
    import json

    from plugin_loader import load_plugin
    from wsp_core import routing

    config_path = tmp_path / "config.json"
    monkeypatch.setenv("WEB_SEARCH_PLUS_CONFIG", str(config_path))
    monkeypatch.setenv("DONSETCH_BIN", donsetch_bin)
    payload = _guidance("search", ["serper", "brave", "tavily"], wsp_config.load_config())
    assert payload["how_to_fix"] == ['provider="donsetch"', "hermes web-search-plus config set-auto-allow donsetch on"]

    wsp = load_plugin()
    parser = argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    for state, routed in (("on", "donsetch"), ("off", "serper")):
        args = parser.parse_args(["config", "set-auto-allow", "donsetch", state, "--config-path", str(config_path)])
        args.func(args)
        config = wsp_config.load_config()
        assert config["auto_routing"]["order"] == "measured"
        assert ("donsetch" in config["auto_routing"]["provider_priority"]) is (state == "on")
        assert routing.route_query("python asyncio taskgroup", config)["provider"] == routed
    assert "donsetch" not in json.loads(config_path.read_text())["auto_routing"].get("provider_priority", [])
