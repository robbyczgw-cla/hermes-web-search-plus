"""5.0.1 regressions: source gate vs user queries, and configs where every search failed."""

from __future__ import annotations

import socket

import pytest

from wsp_core import config as wsp_config
from wsp_core import providers, search
from wsp_core.config import _deepcopy_default_config, provider_configured
from wsp_core.request_gate_v3 import validate_outbound_body

QUERIES = (
    "best synthesizer keyboard",
    "verify the claim that coffee stunts growth",
    "how do plants photosynthesize and provide an answer to hunger",
)


@pytest.mark.parametrize("query", QUERIES)
def test_gate_does_not_scan_user_query_text(query):
    validate_outbound_body("tavily", {"query": query, "include_answer": False})
    validate_outbound_body("linkup", {"q": query, "outputType": "searchResults"})
    validate_outbound_body("exa", {"query": query, "type": "auto"})
    validate_outbound_body("exa", {"query": query, "includeDomains": ["synthesize.example"]})


def test_gate_still_blocks_wsp_authored_answer_shapes():
    with pytest.raises(ValueError):
        validate_outbound_body("tavily", {"query": "q", "include_answer": False, "instructions": "synthesize an answer"})
    with pytest.raises(ValueError):
        validate_outbound_body("tavily", {"query": "synthesize", "include_answer": True})
    with pytest.raises(ValueError):
        validate_outbound_body("linkup", {"q": "synthesize", "outputType": "sourcedAnswer"})
    with pytest.raises(ValueError):
        validate_outbound_body("exa", {"query": "synthesize", "type": "deep"})


@pytest.mark.parametrize("query", QUERIES)
def test_provider_adapters_send_queries_containing_gate_words(monkeypatch, query):
    sent = []

    def fake_request(url, headers, body, timeout=30):
        sent.append(body)
        return {"results": []}

    monkeypatch.setattr(providers, "make_request", fake_request)
    providers.search_tavily(query, "tvly-test-key-123456")
    providers.search_linkup(query, "linkup-test-key-123456")
    providers.search_exa(query, "exa-test-key-123456789")
    assert len(sent) == 3
    assert [body.get("query", body.get("q")) for body in sent] == [query] * 3


# --- A7a: auto routing off without default_provider --------------------------

def _clear_provider_env(monkeypatch):
    for spec in wsp_config.PROVIDER_SPECS.values():
        monkeypatch.delenv(spec.env_var, raising=False)
    monkeypatch.delenv("SEARXNG_INSTANCE_URL", raising=False)
    monkeypatch.delenv("SEARXNG_ALLOW_PRIVATE", raising=False)


def _answer(name):
    def search_fn(query, api_key, max_results=5, **_kwargs):
        return {"provider": name, "query": query, "results": [
            {"title": name, "url": "https://example.com/a", "snippet": f"from {name}"}]}
    return search_fn


def test_auto_routing_off_without_default_provider_uses_configured_priority(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("BRAVE_API_KEY", "brave-test-key-1234567")
    monkeypatch.setattr(providers, "search_brave", _answer("brave"))
    config = _deepcopy_default_config()
    config["auto_routing"]["enabled"] = False
    config["default_provider"] = None

    routing = search.auto_route_provider("latest AI news", config)
    assert routing["provider"] == "brave"
    assert routing["auto_routed"] is False

    result = search.run_search_request(query="latest AI news", no_cache=True, config=config)
    assert result.get("provider") == "brave"
    assert result["results"][0]["snippet"] == "from brave"


def test_auto_routing_off_without_default_or_keys_never_routes_to_none(monkeypatch):
    _clear_provider_env(monkeypatch)
    config = _deepcopy_default_config()
    config["auto_routing"]["enabled"] = False
    config["default_provider"] = None

    routing = search.auto_route_provider("latest AI news", config)
    assert routing["provider"] not in (None, "None")

    result = search.run_search_request(query="latest AI news", no_cache=True, config=config)
    assert "None" not in str(result.get("provider"))
    assert "None" not in str(result.get("error", ""))
    assert result.get("error")  # a clear setup error, not "All providers failed - None"


# --- A7b: blocked SearXNG URL ------------------------------------------------

def _blocked_searxng_config():
    config = _deepcopy_default_config()
    config["searxng"] = {"instance_url": "http://127.0.0.1:8888"}
    return config


def test_blocked_searxng_url_means_not_configured(monkeypatch):
    _clear_provider_env(monkeypatch)
    config = _blocked_searxng_config()
    assert provider_configured("searxng", config) is False
    assert search.provider_configured("searxng", config) is False
    from wsp_core import routing
    assert routing.provider_configured("searxng", config) is False
    monkeypatch.setenv("SEARXNG_ALLOW_PRIVATE", "1")
    assert provider_configured("searxng", config) is True


def test_blocked_searxng_url_does_not_break_auto_search(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("BRAVE_API_KEY", "brave-test-key-1234567")
    monkeypatch.setattr(providers, "search_brave", _answer("brave"))
    config = _blocked_searxng_config()

    routing = search.auto_route_provider("open source speech library", config)
    assert routing["provider"] == "brave"
    assert "searxng" not in routing["candidate_order"]

    result = search.run_search_request(query="open source speech library", no_cache=True, config=config)
    assert result.get("provider") == "brave"


def test_explicit_searxng_with_blocked_url_still_reports_the_hint(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("BRAVE_API_KEY", "brave-test-key-1234567")
    config = _blocked_searxng_config()
    try:
        result = search.run_search_request(query="q", provider="searxng", no_cache=True, config=config)
    except ValueError as exc:
        text = str(exc)
    else:
        text = str(result)
    assert "SEARXNG_ALLOW_PRIVATE" in text


def test_searxng_dns_verdict_is_cached_per_process(monkeypatch):
    _clear_provider_env(monkeypatch)
    wsp_config._SEARXNG_HOST_VERDICTS.clear()
    calls = []

    def fake_getaddrinfo(host, port, *args, **kwargs):
        calls.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.10", port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    config = {"searxng": {"instance_url": "http://searx.lan.test:8888"}}
    try:
        for _ in range(5):
            assert provider_configured("searxng", config) is False
        assert calls == ["searx.lan.test"]
    finally:
        wsp_config._SEARXNG_HOST_VERDICTS.clear()
