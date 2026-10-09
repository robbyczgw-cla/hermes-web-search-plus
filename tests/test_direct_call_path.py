"""The plugin calls the engine directly: no argv, no argparse, no subprocess."""

from __future__ import annotations
from wsp_core import providers

import copy
import subprocess

import pytest

from plugin_loader import load_plugin
from wsp_core import search
from wsp_core.config import DEFAULT_CONFIG

plugin = load_plugin("wsp_plugin_direct_call_path")

# Maintenance-only CLI options; the search pipeline never reads them.
CLI_ONLY = {
    "command", "json", "live", "bench", "bench_providers", "bench_timeout_budget", "no_history",
    "apply", "rollback", "migration_backup_root", "extract_urls", "auto", "explain_routing",
    "compact", "clear_cache", "cache_stats",
}


def _custom_config():
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["defaults"] = {"max_results": 9}
    config["serper"] = {"type": "news"}
    config["tavily"] = {"depth": "advanced", "topic": "news"}
    config["querit"] = {"base_url": "https://querit.example", "base_path": "/v2/search"}
    config["linkup"] = {"depth": "deep"}
    config["exa"] = {"type": "keyword", "verbosity": "full"}
    config["firecrawl"] = {"sources": ["web", "news"]}
    config["you"] = {"safesearch": "strict"}
    config["searxng"] = {"instance_url": "https://searx.example", "safesearch": 2, "engines": ["bing"]}
    return config


@pytest.mark.parametrize("config", [{}, copy.deepcopy(DEFAULT_CONFIG), _custom_config()])
def test_default_search_args_match_the_cli_parser(config):
    parsed = vars(search.build_parser(config).parse_args([]))
    expected = {key: value for key, value in parsed.items() if key not in CLI_ONLY}

    assert vars(search.default_search_args(config)) == expected


def _fake_serper(monkeypatch):
    def fake_search_serper(**kwargs):
        return {"provider": "serper", "query": kwargs.get("query"), "results": [
            {"title": "A", "url": "https://example.com/a", "snippet": "alpha"},
        ]}

    def fake_extract_serper(urls, *_args, **_kwargs):
        return {"provider": "serper", "results": [
            {"url": url, "title": "A", "content": "alpha", "raw_content": "alpha"} for url in urls
        ]}

    monkeypatch.setattr(providers, "search_serper", fake_search_serper)
    monkeypatch.setattr(providers, "extract_serper", fake_extract_serper)
    monkeypatch.setenv("SERPER_API_KEY", "test-key-0123456789")


def test_plugin_search_never_builds_the_cli_parser(monkeypatch):
    _fake_serper(monkeypatch)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("the plugin path must not build the CLI parser")

    monkeypatch.setattr(search, "build_parser", forbidden)

    data = plugin._run_search("direct path query", provider="serper", no_cache=True)

    assert not data.get("error"), data
    assert data["results"][0]["url"] == "https://example.com/a"


@pytest.mark.parametrize("engine_available", [True, False])
def test_tool_handlers_never_start_a_subprocess(monkeypatch, engine_available):
    monkeypatch.setenv("WSP_FORCE_SUBPROCESS", "1")  # retired switch: must change nothing
    _fake_serper(monkeypatch)

    started = []

    def forbidden(*args, **_kwargs):
        started.append(args)  # recorded: handlers swallow exceptions into error dicts
        raise AssertionError("tool handlers must not start a subprocess")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    if not engine_available:
        monkeypatch.setattr(plugin, "_load_search_module", lambda: None)

    searched = plugin._run_search("no subprocess", provider="serper", no_cache=True)
    extracted = plugin._run_extract(["https://example.com/a"], provider="serper")

    assert started == []
    if engine_available:
        assert searched["results"]
        assert extracted["results"][0]["content"] == "alpha"
    else:
        assert searched["error"] and searched["results"] == []
        assert extracted["error"] and extracted["results"] == []


@pytest.mark.parametrize("text", ["--help", "--clear-cache", "--", "-q x", "site:reddit.com"])
def test_option_like_text_reaches_the_provider_as_data(monkeypatch, text):
    # Queries and domains used to travel as argv to a search.py subprocess,
    # where text like "--clear-cache" needed guarding. They are plain data now.
    seen = {}

    def fake_search_serper(**kwargs):
        seen.update(kwargs)
        return {"provider": "serper", "query": kwargs.get("query"), "results": []}

    monkeypatch.setattr(providers, "search_serper", fake_search_serper)
    monkeypatch.setenv("SERPER_API_KEY", "test-key-0123456789")

    # An option-like exclude entry is not a domain: it is skipped, never argv.
    # (As an include entry it is a validation error; see test_domain_filter_translation.)
    plugin._run_search(text, provider="serper", no_cache=True, exclude_domains=[text])

    assert seen["query"] == text
