from wsp_core import providers
import json
from unittest import mock

import pytest


QUERY = "contract test query"
API_KEY = "test-api-key"
RESULT_URL = "https://example.com/result"


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def getheader(self, name):
        return ""


def fake_make_request(url, headers, body, timeout=30):
    if "google.serper.dev" in url:
        return {
            "organic": [{"title": "Serper title", "link": RESULT_URL, "snippet": "Serper snippet"}],
        }
    if "serpbase" in url:
        return {
            "status": 0,
            "organic": [{"title": "SerpBase title", "link": RESULT_URL, "snippet": "SerpBase snippet"}],
        }
    if "tavily.com/search" in url:
        return {
            "results": [{"title": "Tavily title", "url": RESULT_URL, "content": "Tavily snippet", "score": 0.87}],
            "answer": "Tavily answer",
            "images": [],
        }
    if "querit" in url:
        return {
            "error_code": 0,
            "results": {"result": [{"title": "Querit title", "url": RESULT_URL, "snippet": "Querit snippet"}]},
            "search_id": "querit-search-id",
        }
    if "linkup" in url and "search" in url:
        return {
            "results": [{"name": "Linkup title", "url": RESULT_URL, "content": "Linkup snippet"}],
            "answer": "Linkup answer",
            "images": [],
        }
    if "firecrawl" in url and "search" in url:
        return {
            "success": True,
            "data": {"web": [{"title": "Firecrawl title", "url": RESULT_URL, "description": "Firecrawl snippet"}], "images": []},
            "id": "firecrawl-search-id",
        }
    if "exa.ai/search" in url:
        return {
            "results": [{"title": "Exa title", "url": RESULT_URL, "text": "Exa snippet", "score": 0.71}],
        }
    if "parallel.ai/v1/search" in url:
        return {
            "search_id": "parallel-search-id",
            "results": [{"title": "Parallel title", "url": RESULT_URL, "excerpts": [{"text": "Parallel snippet"}]}],
        }
    if "firecrawl" in url and "scrape" in url:
        return {
            "success": True,
            "data": {"metadata": {"title": "Firecrawl extract", "sourceURL": RESULT_URL}, "markdown": "Firecrawl content"},
        }
    if "linkup" in url and "fetch" in url:
        return {"markdown": "Linkup content", "rawHtml": "<p>Linkup content</p>"}
    if "tavily.com/extract" in url:
        return {"results": [{"url": RESULT_URL, "title": "Tavily extract", "raw_content": "Tavily content"}]}
    if "exa.ai/contents" in url:
        return {"results": [{"url": RESULT_URL, "title": "Exa extract", "text": "Exa content"}]}
    if "ydc-index.io/v1/contents" in url:
        return {"results": [{"url": RESULT_URL, "title": "You extract", "markdown": "You content", "metadata": {}}]}
    if "parallel.ai/v1/extract" in url:
        return {"results": [{"url": RESULT_URL, "title": "Parallel extract", "full_content": "Parallel content"}]}
    if "keenable.ai/v1/search" in url:
        return {"results": [{"title": "Keenable title", "url": RESULT_URL, "snippet": "Keenable snippet"}]}
    raise AssertionError(f"Unexpected POST URL in contract test: {url}")


def fake_make_get_request(url, headers):
    if "api.search.brave.com" in url:
        return {
            "web": {"results": [{"title": "Brave title", "url": RESULT_URL, "description": "Brave snippet"}]},
        }
    raise AssertionError(f"Unexpected GET URL in contract test: {url}")


def fake_urlopen(req, timeout=30):
    url = req.full_url
    if "ydc-index.io/v1/search" in url:
        return FakeResponse({
            "results": {"web": [{"title": "You title", "url": RESULT_URL, "snippets": ["You snippet"]}]},
            "metadata": {"search_uuid": "you-search-id"},
        })
    if "/search?" in url:
        return FakeResponse({
            "results": [{"title": "SearXNG title", "url": RESULT_URL, "content": "SearXNG snippet", "score": 1.0}],
            "number_of_results": 1,
        })
    raise AssertionError(f"Unexpected urlopen URL in contract test: {url}")


SEARCH_CASES = [
    ("serper", providers.search_serper, (QUERY, API_KEY), {}),
    ("serpbase", providers.search_serpbase, (QUERY, API_KEY), {}),
    ("brave", providers.search_brave, (QUERY, API_KEY), {}),
    ("tavily", providers.search_tavily, (QUERY, API_KEY), {}),
    ("querit", providers.search_querit, (QUERY, API_KEY), {}),
    ("linkup", providers.search_linkup, (QUERY, API_KEY), {}),
    ("firecrawl", providers.search_firecrawl, (QUERY, API_KEY), {}),
    ("exa", providers.search_exa, (QUERY, API_KEY), {}),
    ("parallel", providers.search_parallel, (QUERY, API_KEY), {}),

    ("you", providers.search_you, (QUERY, API_KEY), {}),
    ("searxng", providers.search_searxng, (QUERY, "https://searxng.example"), {}),
    ("keenable", providers.search_keenable, (QUERY, API_KEY), {}),
]


@pytest.mark.parametrize("provider,func,args,kwargs", SEARCH_CASES)
def test_search_providers_return_common_contract(provider, func, args, kwargs):
    with mock.patch.object(providers, "make_request", side_effect=fake_make_request), \
        mock.patch.object(providers, "make_get_request", side_effect=fake_make_get_request), \
        mock.patch.object(providers, "urlopen", side_effect=fake_urlopen), \
        mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = func(*args, max_results=1, **kwargs)

    assert result["provider"] == provider
    assert result["query"] == QUERY
    assert isinstance(result["results"], list)
    assert "answer" not in result
    assert isinstance(result["images"], list)
    assert isinstance(result["metadata"], dict)

    item = result["results"][0]
    assert isinstance(item["title"], str)
    assert isinstance(item["url"], str)
    assert isinstance(item["snippet"], str)
    assert isinstance(item["score"], (int, float))


EXTRACT_CASES = [
    ("firecrawl", providers.extract_firecrawl),
    ("linkup", providers.extract_linkup),
    ("tavily", providers.extract_tavily),
    ("exa", providers.extract_exa),
    ("you", providers.extract_you),
    ("parallel", providers.extract_parallel),
]


@pytest.mark.parametrize("provider,func", EXTRACT_CASES)
def test_extract_providers_return_common_contract(provider, func):
    with mock.patch.object(providers, "make_request", side_effect=fake_make_request):
        result = func([RESULT_URL], API_KEY)

    assert result["provider"] == provider
    assert isinstance(result["results"], list)
    item = result["results"][0]
    assert item["provider"] == provider
    assert isinstance(item["url"], str)
    assert isinstance(item["title"], str)
    assert isinstance(item["content"], str)
    assert isinstance(item["raw_content"], str)
