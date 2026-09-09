"""4.1.1 adapter fidelity: keep provider evidence and requested filters."""

from datetime import datetime
from unittest import mock

import providers
import search


def test_exa_snippet_prefers_highlights_over_leading_text():
    payload = {
        "results": [
            {
                "title": "Python docs",
                "url": "https://docs.python.org/3/library/functions.html",
                "text": "A" * 800 + " trailing boilerplate that is not the answer.",
                "highlights": [
                    "print() writes the given objects to the text stream.",
                    "flush is a keyword-only argument.",
                ],
                "score": 0.91,
            }
        ]
    }

    with mock.patch.object(providers, "make_request", return_value=payload):
        result = providers.search_exa("print flush", "exa-test-key")

    snippet = result["results"][0]["snippet"]
    assert "print() writes the given objects" in snippet
    assert "flush is a keyword-only argument" in snippet
    assert snippet[:20] != "A" * 20


def test_exa_snippet_falls_back_to_text_when_highlights_missing():
    payload = {
        "results": [
            {
                "title": "Example",
                "url": "https://example.com/page",
                "text": "Leading source sentence without highlights.",
                "highlights": [],
                "score": 0.4,
            }
        ]
    }

    with mock.patch.object(providers, "make_request", return_value=payload):
        result = providers.search_exa("example", "exa-test-key")

    assert result["results"][0]["snippet"] == "Leading source sentence without highlights."


def test_parallel_sends_max_results_and_source_policy_in_advanced_settings():
    fake_response = {
        "search_id": "search_411",
        "results": [
            {
                "title": "Parallel docs",
                "url": "https://docs.parallel.ai/search",
                "excerpts": [{"text": "Search API excerpt."}],
            }
        ],
    }

    with mock.patch.object(providers, "make_request", return_value=fake_response) as mock_request:
        providers.search_parallel(
            "Parallel AI Search API",
            "parallel-test-key",
            max_results=3,
            include_domains=["docs.parallel.ai"],
            exclude_domains=["example.com"],
        )

    _url, _headers, body = mock_request.call_args.args[:3]
    assert body["objective"] == "Parallel AI Search API"
    assert body["search_queries"] == ["Parallel AI Search API"]
    assert "site:" not in body["search_queries"][0]
    assert body["advanced_settings"]["max_results"] == 3
    assert body["advanced_settings"]["source_policy"] == {
        "include_domains": ["docs.parallel.ai"],
        "exclude_domains": ["example.com"],
    }


def test_tavily_search_sends_native_time_range():
    captured = {}

    def fake_request(_endpoint, _headers, body, *args, **kwargs):
        captured["body"] = body
        return {"results": []}

    with mock.patch.object(providers, "make_request", side_effect=fake_request):
        providers.search_tavily(
            "latest tavily changelog",
            "tvly-test",
            time_range="week",
        )

    assert captured["body"]["time_range"] == "week"
    assert captured["body"]["include_answer"] is False


def test_tavily_dispatch_applies_freshness():
    seen = {}

    def fake_tavily(**kwargs):
        seen.update(kwargs)
        return {
            "provider": "tavily",
            "query": kwargs["query"],
            "results": [{"url": "https://example.test/a", "title": "A", "snippet": "s"}],
            "images": [],
            "answer": "",
            "metadata": {},
        }

    with mock.patch.object(search, "provider_in_cooldown", lambda p: (False, 0)):
        with mock.patch.object(search, "cache_get", lambda **kw: None):
            with mock.patch.object(search, "cache_put", lambda **kw: None):
                with mock.patch.object(search, "reset_provider_health", lambda p: None):
                    with mock.patch.dict("os.environ", {"TAVILY_API_KEY": "tavily-test-key"}):
                        with mock.patch.object(search, "search_tavily", fake_tavily):
                            result = search.run_search_request(
                                query="latest tavily changelog",
                                provider="tavily",
                                freshness="week",
                            )

    assert seen["time_range"] == "week"
    assert result["metadata"]["freshness"] == {
        "requested": "week",
        "applied": True,
        "provider": "tavily",
        "native_value": "week",
    }


def _run_tavily_search(**kwargs):
    seen = {}

    def fake_tavily(**call):
        seen.update(call)
        return {
            "provider": "tavily",
            "query": call["query"],
            "results": [{"url": "https://example.test/a", "title": "A", "snippet": "s"}],
            "images": [],
            "answer": "",
            "metadata": {},
        }

    with mock.patch.object(search, "provider_in_cooldown", lambda p: (False, 0)):
        with mock.patch.object(search, "cache_get", lambda **kw: None):
            with mock.patch.object(search, "cache_put", lambda **kw: None):
                with mock.patch.object(search, "reset_provider_health", lambda p: None):
                    with mock.patch.dict("os.environ", {"TAVILY_API_KEY": "tavily-test-key"}):
                        with mock.patch.object(search, "search_tavily", fake_tavily):
                            result = search.run_search_request(
                                query="latest tavily changelog",
                                provider="tavily",
                                **kwargs,
                            )
    return seen, result


def test_tavily_time_range_wins_over_freshness_in_body_and_metadata():
    seen, result = _run_tavily_search(freshness="week", time_range="day")
    assert seen["time_range"] == "day"
    assert result["metadata"]["freshness"] == {
        "requested": "day",
        "applied": True,
        "provider": "tavily",
        "native_value": "day",
    }


def test_tavily_time_range_only_reports_applied_metadata():
    seen, result = _run_tavily_search(time_range="week")
    assert seen["time_range"] == "week"
    assert result["metadata"]["freshness"] == {
        "requested": "week",
        "applied": True,
        "provider": "tavily",
        "native_value": "week",
    }


def _run_exa_search(**kwargs):
    seen = {}

    def fake_exa(**call):
        seen.update(call)
        return {
            "provider": "exa",
            "query": call["query"],
            "results": [{"url": "https://example.test/a", "title": "A", "snippet": "s"}],
            "images": [],
            "answer": "",
            "metadata": {},
        }

    with mock.patch.object(search, "provider_in_cooldown", lambda p: (False, 0)):
        with mock.patch.object(search, "cache_get", lambda **kw: None):
            with mock.patch.object(search, "cache_put", lambda **kw: None):
                with mock.patch.object(search, "reset_provider_health", lambda p: None):
                    with mock.patch.dict("os.environ", {"EXA_API_KEY": "exa-test-key"}):
                        with mock.patch.object(search, "search_exa", fake_exa):
                            result = search.run_search_request(
                                query="latest exa changelog",
                                provider="exa",
                                **kwargs,
                            )
    return seen, result


def _exa_window_hours(native):
    start = datetime.fromisoformat(native["startPublishedDate"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(native["endPublishedDate"].replace("Z", "+00:00"))
    return (end - start).total_seconds() / 3600


def test_exa_time_range_wins_over_freshness_in_body_and_metadata():
    seen, result = _run_exa_search(freshness="week", time_range="day")
    assert seen["freshness"] == "day"
    meta = result["metadata"]["freshness"]
    assert meta["requested"] == "day"
    assert meta["applied"] is True
    assert meta["provider"] == "exa"
    assert 20 <= _exa_window_hours(meta["native_value"]) <= 28


def test_exa_time_range_only_applies_native_freshness():
    seen, result = _run_exa_search(time_range="week")
    assert seen["freshness"] == "week"
    meta = result["metadata"]["freshness"]
    assert meta["requested"] == "week"
    assert meta["applied"] is True
    assert meta["provider"] == "exa"
    assert 160 <= _exa_window_hours(meta["native_value"]) <= 176
