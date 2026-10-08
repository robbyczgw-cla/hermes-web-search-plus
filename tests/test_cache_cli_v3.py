"""`--clear-cache` and `--cache-stats` cover the v3 response cache the tools use."""

from __future__ import annotations

import json
import sys

from wsp_core import providers, search
from wsp_core.cache_v3 import ResponseCacheV3
from wsp_core.config import _deepcopy_default_config


def _config(tmp_path):
    config = _deepcopy_default_config()
    config["auto_routing"]["provider_priority"] = ["serper"]
    config["v3"] = {"cache_dir": str(tmp_path)}
    return config


def _cache_one_search(monkeypatch, config):
    monkeypatch.setenv("SERPER_API_KEY", "serper-test-key-123456")
    monkeypatch.setattr(providers, "search_serper", lambda query, api_key, max_results=5, **_kw: {
        "provider": "serper", "query": query,
        "results": [{"title": "t", "url": "https://example.com/a", "snippet": "s"}]})
    search.run_search_request(query="cache cli test", provider="serper", config=config)
    assert ResponseCacheV3(config["v3"]["cache_dir"]).stats()["entries"] == 1


def _run_cli(monkeypatch, capsys, config, flag):
    monkeypatch.setattr(search, "load_config", lambda: config)
    monkeypatch.setattr(sys, "argv", ["search.py", flag])
    search.main()
    return json.loads(capsys.readouterr().out)


def test_cache_stats_counts_v3_response_entries(monkeypatch, capsys, tmp_path):
    config = _config(tmp_path)
    _cache_one_search(monkeypatch, config)

    stats = _run_cli(monkeypatch, capsys, config, "--cache-stats")

    assert stats["v3_response"]["entries"] == 1


def test_clear_cache_removes_v3_response_entries(monkeypatch, capsys, tmp_path):
    config = _config(tmp_path)
    _cache_one_search(monkeypatch, config)

    result = _run_cli(monkeypatch, capsys, config, "--clear-cache")

    assert result["v3_response_cleared"] == 1
    assert ResponseCacheV3(tmp_path).stats()["entries"] == 0
