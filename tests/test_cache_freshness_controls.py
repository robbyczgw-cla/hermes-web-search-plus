"""Public cache controls, recency TTL caps, and query-ranked summaries."""
from unittest import mock

import cache
import compat_v3
from contract_v3 import Capability

import __init__ as plugin


def test_search_schema_exposes_no_cache_and_cache_ttl():
    registered = {}

    class Ctx:
        def register_tool(self, **kwargs):
            registered[kwargs["name"]] = kwargs

    plugin.register(Ctx())
    props = registered["web_search_plus"]["schema"]["parameters"]["properties"]
    assert props["no_cache"]["type"] == "boolean"
    assert props["no_cache"]["default"] is False
    assert props["cache_ttl"]["type"] == "integer"
    assert props["cache_ttl"]["minimum"] == 1
    assert props["cache_ttl"]["maximum"] == 86400


def test_handler_forwards_no_cache_and_cache_ttl():
    seen = {}

    def fake_run_search(**kwargs):
        seen.update(kwargs)
        return {"provider": "serper", "query": kwargs["query"], "results": []}

    class Ctx:
        def register_tool(self, **kwargs):
            registered[kwargs["name"]] = kwargs["handler"]

    registered = {}
    plugin.register(Ctx())
    with mock.patch.object(plugin, "_run_search", fake_run_search):
        registered["web_search_plus"](
            {
                "query": "latest python release",
                "no_cache": True,
                "cache_ttl": 90,
            }
        )
    assert seen["no_cache"] is True
    assert seen["cache_ttl"] == 90


def test_legacy_projection_honors_plugin_no_cache():
    request = compat_v3.legacy_request_to_v3(
        Capability.SEARCH,
        {"query": "latest python release", "provider": "serper", "no_cache": True},
    )
    assert request.cache["mode"] == "bypass"


def test_recency_ttl_caps_latest_below_default_hour():
    assert cache.effective_search_cache_ttl("latest python release") == 300
    assert cache.effective_search_cache_ttl("breaking news tonight") == 60
    assert cache.effective_search_cache_ttl("bookshelf speakers") == cache.DEFAULT_CACHE_TTL
    assert cache.effective_search_cache_ttl("python docs", freshness="day") == 300
    assert cache.effective_search_cache_ttl("python docs", freshness="week") == 1800
    assert cache.effective_search_cache_ttl("python docs", requested_ttl=120) == 120
    assert cache.effective_search_cache_ttl("latest python", requested_ttl=3600) == 300


def test_auto_routed_formatter_keeps_cache_age_visible():
    output = plugin._format_results(
        {
            "provider": "serper",
            "query": "latest python release",
            "cached": True,
            "cache_age_seconds": 1847,
            "routing": {
                "auto_routed": True,
                "confidence_level": "high",
                "reason": "shopping",
            },
            "results": [],
        }
    )
    assert "cached 1847s ago" in output
    assert "recency query" in output


def test_research_summary_uses_query_ranked_span_not_prefix():
    nav = "Skip to content. Home About Careers. " * 20
    hit = "CEO of ExampleCorp is Ada Lovelace as of 2026."
    content = nav + hit + (" footer " * 40)
    assert len(content) > 500
    assert content[:500].find("Ada Lovelace") == -1

    output = plugin._format_results(
        {
            "provider": "research",
            "query": "ExampleCorp current CEO",
            "results": [
                {
                    "title": "ExampleCorp",
                    "url": "https://example.com/about",
                    "snippet": "About",
                }
            ],
            "source_summaries": [{"url": "https://example.com/about", "content": content}],
        }
    )
    assert "Ada Lovelace" in output
    assert "query-ranked" in output
    assert "showing first 500" not in output
