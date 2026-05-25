import importlib.util
import json
from pathlib import Path
from unittest import mock

import pytest

SEARCH_PATH = Path(__file__).resolve().parents[1] / "search.py"
search_spec = importlib.util.spec_from_file_location("wsp_search_contracts_under_test", SEARCH_PATH)
search = importlib.util.module_from_spec(search_spec)
assert search_spec.loader is not None
search_spec.loader.exec_module(search)

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
    if "perplexity" in url or "kilo" in url:
        return {
            "choices": [{"message": {"content": "Perplexity answer with https://example.com/source"}}],
            "citations": [{"url": RESULT_URL, "title": "Perplexity source"}],
            "usage": {},
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
    ("serper", search.search_serper, (QUERY, API_KEY), {}),
    ("serpbase", search.search_serpbase, (QUERY, API_KEY), {}),
    ("brave", search.search_brave, (QUERY, API_KEY), {}),
    ("tavily", search.search_tavily, (QUERY, API_KEY), {}),
    ("querit", search.search_querit, (QUERY, API_KEY), {}),
    ("linkup", search.search_linkup, (QUERY, API_KEY), {}),
    ("firecrawl", search.search_firecrawl, (QUERY, API_KEY), {}),
    ("exa", search.search_exa, (QUERY, API_KEY), {}),
    ("parallel", search.search_parallel, (QUERY, API_KEY), {}),
    ("perplexity", search.search_perplexity, (QUERY, API_KEY), {}),
    ("kilo-perplexity", search.search_perplexity, (QUERY, API_KEY), {"provider_name": "kilo-perplexity", "api_url": "https://api.kilo.ai/openai/v1/chat/completions", "model": "perplexity/sonar-pro"}),
    ("you", search.search_you, (QUERY, API_KEY), {}),
    ("searxng", search.search_searxng, (QUERY, "https://searxng.example"), {}),
]


@pytest.mark.parametrize("provider,func,args,kwargs", SEARCH_CASES)
def test_search_providers_return_common_contract(provider, func, args, kwargs):
    with mock.patch.object(search, "make_request", side_effect=fake_make_request), \
        mock.patch.object(search, "make_get_request", side_effect=fake_make_get_request), \
        mock.patch.object(search._providers, "urlopen", side_effect=fake_urlopen), \
        mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = func(*args, max_results=1, **kwargs)

    assert result["provider"] == provider
    assert result["query"] == QUERY
    assert isinstance(result["results"], list)
    assert isinstance(result["answer"], str)
    assert isinstance(result["images"], list)
    assert isinstance(result["metadata"], dict)

    item = result["results"][0]
    assert isinstance(item["title"], str)
    assert isinstance(item["url"], str)
    assert isinstance(item["snippet"], str)
    assert isinstance(item["score"], (int, float))


EXTRACT_CASES = [
    ("firecrawl", search.extract_firecrawl),
    ("linkup", search.extract_linkup),
    ("tavily", search.extract_tavily),
    ("exa", search.extract_exa),
    ("you", search.extract_you),
    ("parallel", search.extract_parallel),
]


@pytest.mark.parametrize("provider,func", EXTRACT_CASES)
def test_extract_providers_return_common_contract(provider, func):
    with mock.patch.object(search, "make_request", side_effect=fake_make_request):
        result = func([RESULT_URL], API_KEY)

    assert result["provider"] == provider
    assert isinstance(result["results"], list)
    item = result["results"][0]
    assert item["provider"] == provider
    assert isinstance(item["url"], str)
    assert isinstance(item["title"], str)
    assert isinstance(item["content"], str)
    assert isinstance(item["raw_content"], str)
