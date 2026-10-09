"""Users can replace automatic routing by query type with their own order."""
from __future__ import annotations

import pytest

from wsp_core import config as cfg
from wsp_core import routing


@pytest.fixture(autouse=True)
def _no_real_keys(monkeypatch):
    import os
    for name in list(os.environ):
        if name.endswith("_API_KEY") or name.endswith("_ALLOW_PUBLIC") or name.startswith("SEARXNG"):
            monkeypatch.delenv(name, raising=False)


def _config(order=None, priority=None, keys=("brave", "serper", "exa")):
    config = cfg._deepcopy_default_config()
    for provider in keys:
        config.setdefault(provider, {})["api_key"] = f"fake-{provider}-key"
    if order:
        config["auto_routing"]["order"] = order
    if priority:
        config["auto_routing"]["provider_priority"] = list(priority)
    return cfg._validate_runtime_config(config)


@pytest.mark.parametrize("query", ["python dataclasses documentation", "best budget headphones", "latest EU AI act news"])
def test_custom_order_uses_users_first_provider_for_every_intent(query):
    decision = routing.route_query(query, _config("custom", ["exa", "serper", "brave"]))
    assert decision["provider"] == "exa"
    assert decision["reason"] == "custom_order"
    assert decision["candidate_order"][:3] == ["exa", "serper", "brave"]


def test_custom_order_skips_providers_without_key():
    decision = routing.route_query("anything", _config("custom", ["tavily", "serper", "brave"]))
    assert decision["provider"] == "serper"


def test_measured_order_is_the_default():
    config = _config(priority=["serper", "exa", "brave"])
    assert config["auto_routing"]["order"] == "measured"
    decision = routing.route_query("best budget headphones under 100", config)
    assert decision["reason"] != "custom_order"
    assert decision["provider"] == "serper"  # shopping rule, not the user's list order


def test_unknown_order_value_falls_back_to_measured():
    assert _config("chaos")["auto_routing"]["order"] == "measured"


def test_desktop_setting_sets_and_resets_custom_order():
    config = cfg._deepcopy_default_config()
    cfg._apply_desktop_settings(config, {"provider_order": "Exa, Serper"})
    assert config["auto_routing"]["order"] == "custom"
    assert config["auto_routing"]["provider_priority"][:2] == ["exa", "serper"]
    cfg._apply_desktop_settings(config, {"provider_order": "auto"})
    assert config["auto_routing"]["order"] == "measured"
    cfg._apply_desktop_settings(config, {"provider_order": ""})
    assert config["auto_routing"]["order"] == "measured"


def test_cli_set_order_persists_and_resets(tmp_path, monkeypatch):
    import argparse
    import json
    from plugin_loader import load_plugin

    wsp = load_plugin("wsp_plugin_custom_order_under_test")

    path = tmp_path / "config.json"
    parser = argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(["config", "set-order", "serper,brave", "--config-path", str(path)])
    args.func(args)
    auto = json.loads(path.read_text())["auto_routing"]
    assert auto["order"] == "custom"
    assert auto["provider_priority"][:2] == ["serper", "brave"]
    args = parser.parse_args(["config", "set-order", "auto", "--config-path", str(path)])
    args.func(args)
    assert json.loads(path.read_text())["auto_routing"]["order"] == "measured"
