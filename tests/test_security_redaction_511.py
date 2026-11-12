"""Operator JSON previews must not disclose credential material."""
import json

import pytest

from plugin_loader import load_plugin
from wsp_core import credentials

plugin = load_plugin("wsp_security_redaction_511")


@pytest.mark.parametrize("name", ["key", "api-key", "x-api-key", "api_keys", "access_key", "private_key", "auth", "credentials"])
@pytest.mark.parametrize("value", ["fixture-secret", {"value": "fixture-secret"}, ["fixture-secret"]])
def test_secret_names_mask_whole_subtrees(name, value):
    assert credentials.redact_secrets({name: value}) == {name: "set"}


@pytest.mark.parametrize("name", ["keyless", "keyless_public", "allow_public", "max_results", "max_tokens", "api_key_file", "keyboard"])
def test_non_secret_names_are_preserved(name):
    assert credentials.redact_secrets({name: "keep"}) == {name: "keep"}


@pytest.mark.parametrize("argv", [["status", "--json"], ["config", "show", "--json"], ["config", "set-fallback", "tavily", "--dry-run"], ["config", "reset", "--dry-run"]])
def test_cli_redacts_url_credentials_and_previews_only_changes(tmp_path, monkeypatch, capsys, argv):
    monkeypatch.setattr(plugin, "_donsetch_status", lambda *a: {})
    monkeypatch.setattr(plugin, "_get_hermes_config_path", lambda: tmp_path / "missing.yaml")
    path = tmp_path / "config.json"
    config = plugin._default_behavior_config()
    config.update({"searxng": {"base_url": "https://fixture-user:fixture-password@example.com/path?token=fixture-token"}, "custom": {"credentials": {"value": "fixture-secret"}}})
    config["auto_routing"]["fallback_provider"] = "brave"
    path.write_text(json.dumps(config), encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    parser = plugin.argparse.ArgumentParser()
    plugin._web_search_plus_cli_setup(parser)
    args = parser.parse_args([*argv, "--config-path", str(path)])
    args.func(args)
    output = capsys.readouterr().out
    assert not any(value in output for value in ("fixture-user", "fixture-password", "fixture-token", "fixture-secret"))
    payload = json.loads(output)
    if "--dry-run" in argv:
        assert "searxng" not in payload and "custom" not in payload
        assert set(payload) == {"auto_routing"}
        assert set(payload["auto_routing"]) == {"fallback_provider"}
    else:
        data = payload.get("routing", payload)
        assert data["searxng"]["base_url"] == "https://example.com/path"
    assert path.read_text(encoding="utf-8") == before
