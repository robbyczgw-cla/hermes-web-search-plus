"""4.1.1 adapter fidelity: keep provider evidence and requested filters."""

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
