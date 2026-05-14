from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


SEARCH_PATH = Path(__file__).resolve().parents[1] / "search.py"
search_spec = importlib.util.spec_from_file_location("wsp_search_perplexity_under_test", SEARCH_PATH)
search = importlib.util.module_from_spec(search_spec)
assert search_spec.loader is not None
search_spec.loader.exec_module(search)

PLUGIN_PATH = Path(__file__).resolve().parents[1] / "__init__.py"
plugin_spec = importlib.util.spec_from_file_location("wsp_plugin_perplexity_under_test", PLUGIN_PATH)
wsp = importlib.util.module_from_spec(plugin_spec)
assert plugin_spec.loader is not None
plugin_spec.loader.exec_module(wsp)


def test_perplexity_default_uses_native_api_endpoint_and_model(monkeypatch):
    captured = {}

    def fake_request(api_url, headers, body):
        captured["api_url"] = api_url
        captured["headers"] = headers
        captured["body"] = body
        return {"choices": [{"message": {"content": "answer"}}], "citations": []}

    monkeypatch.setattr(search, "make_request", fake_request)

    result = search.search_perplexity(query="latest ai news", api_key="pplx-test-key")

    assert result["provider"] == "perplexity"
    assert captured["api_url"] == "https://api.perplexity.ai/chat/completions"
    assert captured["body"]["model"] == "sonar-pro"
    assert captured["headers"]["Authorization"] == "Bearer pplx-test-key"


def test_kilo_perplexity_is_distinct_routing_provider_with_kilo_key(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"version": 1, "default_provider": "kilo-perplexity", "auto_routing": {"enabled": False}}))
    monkeypatch.setenv("WEB_SEARCH_PLUS_CONFIG", str(config_path))
    monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
    monkeypatch.setenv("KILOCODE_API_KEY", "kilo-test-key")
    monkeypatch.setattr(search, "provider_in_cooldown", lambda _provider: (False, 0))
    monkeypatch.setattr(search, "execute_provider_with_retry", lambda _provider, fn: fn())
    monkeypatch.setattr(sys, "argv", ["search.py", "--query", "hello", "--provider", "auto", "--no-cache"])

    captured = {}

    def fake_search_perplexity(**kwargs):
        captured.update(kwargs)
        return {"provider": "kilo-perplexity", "query": kwargs["query"], "results": [], "images": []}

    monkeypatch.setattr(search, "search_perplexity", fake_search_perplexity)

    search.main()

    data = json.loads(capsys.readouterr().out)
    assert data["provider"] == "kilo-perplexity"
    assert captured["api_key"] == "kilo-test-key"
    assert captured["model"] == "perplexity/sonar-pro"
    assert captured["api_url"] == "https://api.kilo.ai/api/gateway/chat/completions"


def test_native_perplexity_uses_perplexity_key_not_kilo_key(monkeypatch):
    monkeypatch.setenv("PERPLEXITY_API_KEY", "pplx-native-key")
    monkeypatch.setenv("KILOCODE_API_KEY", "kilo-bridge-key")

    assert search.get_api_key("perplexity") == "pplx-native-key"
    assert search.get_api_key("kilo-perplexity") == "kilo-bridge-key"


def test_onboarding_routing_keeps_kilo_perplexity_distinct(tmp_path):
    config_path = tmp_path / "config.json"
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(["config", "set-default", "kilo-perplexity", "--config-path", str(config_path)])

    args.func(args)

    data = json.loads(config_path.read_text())
    assert data["default_provider"] == "kilo-perplexity"
