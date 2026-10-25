"""Machine-readable ``key=value`` bits at the end of the search header.

Existing bits keep their wording and order; the new ones are appended.
"""

import re

import pytest

from plugin_loader import load_plugin
from wsp_core import providers, search
from wsp_core.config import _deepcopy_default_config

plugin = load_plugin("wsp_plugin_test_header_key_value_bits")

RESULT = {"title": "T", "url": "https://a.example/1", "snippet": "s"}


def _header(data):
    return plugin._format_results(data).splitlines()[1]


def test_results_bit_counts_rendered_results():
    assert _header({"provider": "brave", "results": [RESULT, RESULT]}) == "[Provider: brave | results=2]"


def test_results_bit_is_zero_on_a_successful_empty_call():
    assert _header({"provider": "brave", "results": []}) == "[Provider: brave | results=0]"


def test_elapsed_and_fallback_bits_are_appended_after_the_human_bits():
    data = {
        "provider": "brave", "results": [RESULT], "elapsed_ms": 842, "fallback_from": ["serper", "exa"],
        "routing": {"auto_routed": True, "confidence_level": "low", "reason": "intent_general"},
    }
    assert _header(data) == (
        "[Provider: brave | auto-routed | low confidence | intent_general"
        " | results=1 | elapsed_ms=842 | fallback_from=serper,exa]"
    )


@pytest.mark.parametrize("value", [None, -1, True, "12", 1.5])
def test_elapsed_bit_is_omitted_when_unknown(value):
    assert "elapsed_ms" not in _header({"provider": "brave", "results": [], "elapsed_ms": value})


def test_fallback_bit_is_omitted_without_a_fallback():
    for value in (None, [], "serper", [""]):
        assert "fallback_from" not in _header({"provider": "brave", "results": [], "fallback_from": value})


def test_fallback_ids_are_single_tokens_and_deduplicated():
    header = _header({"provider": "brave", "results": [], "fallback_from": ["a b|c]=d", "a b|c]=d", "ok-1"]})
    assert header.endswith("| fallback_from=abcd,ok-1]")
    assert header.count("[") == 1 and header.count("]") == 1


def test_cache_hit_keeps_the_routing_origin_of_the_entry():
    data = {
        "provider": "keenable", "cached": True, "cache_age_seconds": 7, "results": [RESULT], "elapsed_ms": 3,
        "cache_origin_routing": {"auto_routed": True, "confidence_level": "low", "reason": "intent_general"},
    }
    assert _header(data) == (
        "[Provider: keenable | cached 7s ago | auto-routed | low confidence | intent_general"
        " | results=1 | elapsed_ms=3]"
    )


def test_cache_hit_without_origin_adds_no_routing_bits():
    data = {"provider": "keenable", "cached": True, "cache_age_seconds": 7, "results": [RESULT],
            "cache_origin_routing": {"auto_routed": False}}
    assert _header(data) == "[Provider: keenable | cached 7s ago | results=1]"
    del data["cache_origin_routing"]
    assert _header(data) == "[Provider: keenable | cached 7s ago | results=1]"


def test_fresh_result_ignores_a_stray_cache_origin():
    data = {"provider": "keenable", "results": [RESULT],
            "cache_origin_routing": {"auto_routed": True, "reason": "intent_news"}}
    assert _header(data) == "[Provider: keenable | results=1]"


def test_research_header_lists_providers_and_pages():
    data = {
        "provider": "research", "results": [RESULT], "elapsed_ms": 12400,
        "routing": {"auto_routed": True, "confidence_level": "low", "reason": "intent_general"},
        "metadata": {"providers_merged": ["brave", "exa", "brave", "keenable"], "extracted_url_count": 3},
    }
    assert _header(data) == (
        "[Provider: research | auto-routed | low confidence | intent_general"
        " | providers=brave,exa,keenable | pages=3 | results=1 | elapsed_ms=12400]"
    )


def test_research_header_omits_unknown_summary_bits():
    data = {"provider": "research", "results": [], "metadata": {"providers_merged": [], "extracted_url_count": None}}
    assert _header(data) == "[Provider: research | results=0]"


def test_non_research_header_ignores_research_metadata():
    data = {"provider": "brave", "results": [], "metadata": {"providers_merged": ["brave", "exa"]}}
    assert _header(data) == "[Provider: brave | results=0]"


def test_error_output_has_no_header_bits():
    out = plugin._format_results({"error": "x", "results": [], "elapsed_ms": 5, "fallback_from": ["a"]})
    assert out == "Search error: x"


# --- through the engine and the tool handler --------------------------------

KEYS = {"SERPER_API_KEY": "serper-test-key-123456", "BRAVE_API_KEY": "brave-test-key-1234567",
        "EXA_API_KEY": "exa-test-key-123456789"}


def _config():
    config = _deepcopy_default_config()
    config["auto_routing"]["provider_priority"] = ["serper", "brave", "exa"]
    config["v3"] = {"hedge_min_delay_seconds": 0.2, "attempt_timeout_seconds": 5}
    return config


def _answer(name):
    def search_fn(query, api_key, max_results=5, **_kwargs):
        return {"provider": name, "query": query, "results": [
            {"title": name, "url": f"https://{name}.example/a", "snippet": f"from {name}"}]}
    return search_fn


def _empty(query, api_key, max_results=5, **_kwargs):
    return {"provider": "x", "query": query, "results": []}


def _boom(query, api_key, max_results=5, **_kwargs):
    raise RuntimeError("boom")


@pytest.fixture
def handler(monkeypatch):
    for name, value in KEYS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(search, "auto_route_provider", lambda query, config: {
        "provider": "serper", "confidence": 0.3, "confidence_level": "low", "reason": "intent_general",
        "routing_policy": "routing-v3", "scores": {"serper": 3.0}, "top_signals": [],
        "analysis_summary": {"routing_class": "general"},
    })
    monkeypatch.setattr(search, "load_config", _config)
    tools = {}

    class Ctx:
        def register_tool(self, **kwargs):
            tools[kwargs["name"]] = kwargs["handler"]

    plugin.register(Ctx())
    return tools["web_search_plus"]


def test_handler_reports_elapsed_and_the_failed_providers_before_the_serving_one(monkeypatch, handler):
    monkeypatch.setattr(providers, "search_serper", _boom)
    monkeypatch.setattr(providers, "search_brave", _empty)
    monkeypatch.setattr(providers, "search_exa", _answer("exa"))

    header = handler({"query": "fallback chain", "no_cache": True}).splitlines()[1]

    assert re.fullmatch(
        r"\[Provider: exa \| auto-routed \| low confidence \| intent_general"
        r" \| results=1 \| elapsed_ms=\d+ \| fallback_from=serper,brave\]", header), header


def test_handler_header_has_no_fallback_bit_when_the_first_provider_serves(monkeypatch, handler):
    monkeypatch.setattr(providers, "search_serper", _answer("serper"))

    header = handler({"query": "direct hit", "no_cache": True}).splitlines()[1]

    assert re.fullmatch(
        r"\[Provider: serper \| auto-routed \| low confidence \| intent_general"
        r" \| results=1 \| elapsed_ms=\d+\]", header), header


def test_handler_cache_hit_keeps_origin_and_reports_its_own_time(monkeypatch, handler):
    monkeypatch.setattr(providers, "search_serper", _answer("serper"))
    first = handler({"query": "cache origin"}).splitlines()[1]
    second = handler({"query": "cache origin"}).splitlines()[1]

    assert "cached" not in first
    assert re.fullmatch(
        r"\[Provider: serper \| cached \d+s ago \| auto-routed \| low confidence \| intent_general"
        r" \| results=1 \| elapsed_ms=\d+\]", second), second


def test_engine_result_dict_is_unchanged_by_the_header_facts(monkeypatch, handler):
    monkeypatch.setattr(providers, "search_serper", _answer("serper"))
    trace = {}
    kwargs = dict(query="json stays", config=_config(), trace=trace)
    search.run_search_request(**kwargs)
    hit = search.run_search_request(**kwargs)
    plain = search.run_search_request(query="json stays", config=_config())

    # Both are cache hits; their age can differ by a second on a slow run.
    assert {k: v for k, v in hit.items() if k != "cache_age_seconds"} == {
        k: v for k, v in plain.items() if k != "cache_age_seconds"
    }
    assert hit["cached"] is True and "routing" not in hit
    assert not any(key.startswith("_v3_") or key in {"trace", "elapsed_ms", "fallback_from"} for key in hit)
    assert trace["cache_origin_routing"] == {"auto_routed": True, "confidence_level": "low", "reason": "intent_general"}


def test_trace_counts_failed_and_empty_attempts_but_not_cancelled_hedges():
    from types import SimpleNamespace

    from wsp_core.search import _fill_search_trace

    def attempt(provider, outcome, result_count=0):
        return SimpleNamespace(provider=provider, outcome=outcome, result_count=result_count, decision="attempted")

    execution = SimpleNamespace(
        response=SimpleNamespace(provider_attempts=[
            attempt("serper", "failed"),
            attempt("tavily", "cancelled"),
            attempt("brave", "success", 0),
            attempt("exa", "success", 3),
        ]),
        legacy_payload={},
    )
    trace = {}
    _fill_search_trace(trace, execution, {"provider": "exa"})
    assert trace == {"fallback_from": ["serper", "brave"]}
