"""Serper news vertical and webpage-scraper extraction coverage.

Locks down the /news endpoint parsing (results live under "news", not
"organic"), the unified search_type parameter contract (validated values,
applied/not-applied metadata per provider), and extract_serper against the
scrape.serper.dev webpage scraper (per-URL error items, endpoint override).
All HTTP is mocked; no network.
"""

import contextlib
import os
import unittest
from unittest import mock

import providers
import search


def _canned(provider):
    return {
        "provider": provider,
        "query": "q",
        "results": [{"url": "https://example.test/a", "title": "A", "snippet": "s"}],
        "images": [],
        "answer": "",
        "metadata": {},
    }


_FAKE_NEWS_RESPONSE = {
    "news": [
        {
            "title": "Turntable maker announces new flagship",
            "link": "https://news.example.test/flagship",
            "snippet": "The new deck ships in autumn.",
            "date": "2 hours ago",
            "source": "HiFi News",
            "imageUrl": "https://news.example.test/flagship.jpg",
            "position": 1,
        },
        {
            "title": "Vinyl sales keep climbing",
            "link": "https://news.example.test/vinyl",
            "snippet": "Another record year.",
            "date": "1 day ago",
            "source": "Audio Weekly",
            "position": 2,
        },
    ]
}


class SerperNewsParsingTests(unittest.TestCase):
    def test_news_search_hits_news_endpoint_and_parses_news_field(self):
        captured = {}

        def fake_post(url, headers, body, timeout=30):
            captured["url"] = url
            captured["headers"] = headers
            captured["body"] = body
            return _FAKE_NEWS_RESPONSE

        with mock.patch("search.make_request", side_effect=fake_post):
            result = search.search_serper(query="hifi news", api_key="serper-key", search_type="news")

        self.assertEqual(captured["url"], "https://google.serper.dev/news")
        self.assertEqual(captured["headers"]["X-API-KEY"], "serper-key")
        self.assertEqual(captured["body"]["q"], "hifi news")

        self.assertEqual(result["provider"], "serper")
        self.assertEqual(len(result["results"]), 2)
        first = result["results"][0]
        self.assertEqual(first["title"], "Turntable maker announces new flagship")
        self.assertEqual(first["url"], "https://news.example.test/flagship")
        self.assertEqual(first["snippet"], "The new deck ships in autumn.")
        self.assertEqual(first["date"], "2 hours ago")
        self.assertEqual(first["source"], "HiFi News")
        self.assertEqual(first["thumbnail"], "https://news.example.test/flagship.jpg")
        self.assertEqual(first["position"], 1)
        self.assertAlmostEqual(first["score"], 1.0)
        # Second item has no imageUrl: thumbnail key must be absent, not empty.
        self.assertNotIn("thumbnail", result["results"][1])

    def test_news_search_keeps_tbs_freshness_filter(self):
        captured = {}

        def fake_post(url, headers, body, timeout=30):
            captured["url"] = url
            captured["body"] = body
            return _FAKE_NEWS_RESPONSE

        with mock.patch("search.make_request", side_effect=fake_post):
            search.search_serper(query="q", api_key="k", search_type="news", time_range="week")

        self.assertEqual(captured["url"], "https://google.serper.dev/news")
        self.assertEqual(captured["body"]["tbs"], "qdr:w")

    def test_regular_search_still_parses_organic(self):
        fake = {"organic": [{"title": "T", "link": "https://example.test", "snippet": "s"}]}
        with mock.patch("search.make_request", return_value=fake):
            result = search.search_serper(query="q", api_key="k")
        self.assertEqual(result["results"][0]["url"], "https://example.test")
        self.assertNotIn("source", result["results"][0])

    def test_news_search_with_organic_only_payload_returns_empty_not_crash(self):
        # Defensive: a payload without "news" yields no results instead of
        # silently reading the wrong field.
        with mock.patch("search.make_request", return_value={"organic": [{"title": "T", "link": "u"}]}):
            result = search.search_serper(query="q", api_key="k", search_type="news")
        self.assertEqual(result["results"], [])


class SearchTypeContractTests(unittest.TestCase):
    def test_support_table(self):
        self.assertEqual(providers.SEARCH_TYPE_VALUES, ("search", "news"))
        self.assertEqual(providers.PROVIDER_SEARCH_TYPES, {"serper": {"search": "search", "news": "news"}})
        self.assertTrue(providers.provider_supports_search_type("serper", "news"))
        self.assertFalse(providers.provider_supports_search_type("tavily", "news"))

    def test_normalize_search_type(self):
        self.assertEqual(providers.normalize_search_type("NEWS"), "news")
        self.assertEqual(providers.normalize_search_type(" search "), "search")
        self.assertIsNone(providers.normalize_search_type(None))
        self.assertIsNone(providers.normalize_search_type(""))
        with self.assertRaises(ValueError) as ctx:
            providers.normalize_search_type("shopping")
        self.assertIn("shopping", str(ctx.exception))
        self.assertIn("search, news", str(ctx.exception))

    def test_search_type_metadata_shapes(self):
        applied = providers.search_type_metadata("serper", "news")
        self.assertEqual(applied, {
            "requested": "news",
            "applied": True,
            "provider": "serper",
            "native_value": "news",
        })
        skipped = providers.search_type_metadata("tavily", "news")
        self.assertEqual(skipped, {
            "requested": "news",
            "applied": False,
            "provider": "tavily",
            "reason": "provider tavily does not support search_type news",
        })

    def test_cli_parser_lowercases_search_type(self):
        parser = search.build_parser({})
        args = parser.parse_args(["--query", "q", "--search-type", "NEWS"])
        self.assertEqual(args.search_type, "news")

    def test_cli_parser_defaults_to_config_type(self):
        parser = search.build_parser({})
        args = parser.parse_args(["--query", "q"])
        self.assertEqual(args.search_type, "search")
        parser = search.build_parser({"serper": {"type": "news"}})
        args = parser.parse_args(["--query", "q"])
        self.assertEqual(args.search_type, "news")


class SearchTypePipelineTests(unittest.TestCase):
    def _isolate(self, stack):
        stack.enter_context(mock.patch.object(search, "provider_in_cooldown", lambda p: (False, 0)))
        stack.enter_context(mock.patch.object(search, "cache_get", lambda **kw: None))
        stack.enter_context(mock.patch.object(search, "cache_put", lambda **kw: None))
        stack.enter_context(mock.patch.object(search, "reset_provider_health", lambda p: None))

    def test_invalid_search_type_returns_error_dict(self):
        result = search.run_search_request(query="q", provider="serper", search_type="shopping")
        self.assertIn("Invalid search_type value", result["error"])
        self.assertEqual(result["results"], [])

    def test_news_reaches_serper_and_metadata_reports_applied(self):
        with contextlib.ExitStack() as stack:
            self._isolate(stack)
            stack.enter_context(mock.patch.dict("os.environ", {"SERPER_API_KEY": "serper-test-key"}))
            seen = {}

            def fake_serper(**kwargs):
                seen.update(kwargs)
                return _canned("serper")

            stack.enter_context(mock.patch.object(search, "search_serper", fake_serper))
            result = search.run_search_request(query="latest hifi news", provider="serper", search_type="NEWS")

        self.assertEqual(seen["search_type"], "news")
        self.assertEqual(result["metadata"]["search_type"], {
            "requested": "news",
            "applied": True,
            "provider": "serper",
            "native_value": "news",
        })

    def test_unsupported_provider_still_searches_and_reports_not_applied(self):
        with contextlib.ExitStack() as stack:
            self._isolate(stack)
            stack.enter_context(mock.patch.dict("os.environ", {"TAVILY_API_KEY": "tavily-test-key"}))
            stack.enter_context(mock.patch.object(search, "search_tavily", lambda **kw: _canned("tavily")))
            result = search.run_search_request(query="how does https work", provider="tavily", search_type="news")

        self.assertEqual(result["results"][0]["url"], "https://example.test/a")
        self.assertEqual(result["metadata"]["search_type"], {
            "requested": "news",
            "applied": False,
            "provider": "tavily",
            "reason": "provider tavily does not support search_type news",
        })

    def test_default_search_type_adds_no_metadata(self):
        with contextlib.ExitStack() as stack:
            self._isolate(stack)
            stack.enter_context(mock.patch.dict("os.environ", {"SERPER_API_KEY": "serper-test-key"}))
            stack.enter_context(mock.patch.object(search, "search_serper", lambda **kw: _canned("serper")))
            result = search.run_search_request(query="plain query", provider="serper")

        self.assertNotIn("search_type", result.get("metadata", {}))

    def test_news_search_type_reaches_serper_request_body(self):
        captured = {}

        def fake_post(url, headers, body, timeout=30):
            captured["url"] = url
            captured["body"] = body
            return _FAKE_NEWS_RESPONSE

        with contextlib.ExitStack() as stack:
            self._isolate(stack)
            stack.enter_context(mock.patch.dict("os.environ", {"SERPER_API_KEY": "serper-test-key"}))
            stack.enter_context(mock.patch("search.make_request", side_effect=fake_post))
            result = search.run_search_request(query="hifi", provider="serper", search_type="news", freshness="day")

        self.assertEqual(captured["url"], "https://google.serper.dev/news")
        self.assertEqual(captured["body"]["tbs"], "qdr:d")
        self.assertEqual(result["results"][0]["source"], "HiFi News")
        self.assertTrue(result["metadata"]["search_type"]["applied"])
        self.assertTrue(result["metadata"]["freshness"]["applied"])


_FAKE_SCRAPE_RESPONSE = {
    "text": "Plain text body",
    "markdown": "# Heading\n\nMarkdown body",
    "metadata": {"title": "Example Page", "og:site_name": "Example"},
    "jsonld": {"@type": "Article"},
    "credits": 1,
}


class SerperExtractTests(unittest.TestCase):
    def setUp(self):
        search.reset_provider_health("serper")

    def tearDown(self):
        search.reset_provider_health("serper")

    def test_extract_success_prefers_markdown(self):
        with mock.patch("search.make_request", return_value=_FAKE_SCRAPE_RESPONSE) as mock_request:
            result = search.extract_serper(["https://example.com/article"], "serper-key")

        self.assertEqual(result["provider"], "serper")
        item = result["results"][0]
        self.assertEqual(item["url"], "https://example.com/article")
        self.assertEqual(item["title"], "Example Page")
        self.assertEqual(item["content"], "# Heading\n\nMarkdown body")
        self.assertEqual(item["raw_content"], "# Heading\n\nMarkdown body")
        self.assertEqual(item["credits"], 1)
        self.assertEqual(item["metadata"]["og:site_name"], "Example")
        self.assertNotIn("error", item)

        url, headers, body = mock_request.call_args.args[:3]
        self.assertEqual(url, "https://scrape.serper.dev")
        self.assertEqual(headers["X-API-KEY"], "serper-key")
        self.assertEqual(body, {"url": "https://example.com/article", "includeMarkdown": True})

    def test_extract_falls_back_to_text_without_markdown(self):
        with mock.patch("search.make_request", return_value={"text": "only text", "credits": 1}):
            result = search.extract_serper(["https://example.com"], "k")
        self.assertEqual(result["results"][0]["content"], "only text")

    def test_extract_reports_error_per_url_and_continues(self):
        responses = iter([Exception("boom"), _FAKE_SCRAPE_RESPONSE])

        def fake_post(url, headers, body, timeout=30):
            value = next(responses)
            if isinstance(value, Exception):
                raise value
            return value

        with mock.patch("search.make_request", side_effect=fake_post):
            result = search.extract_serper(["https://bad.example.com", "https://good.example.com"], "k")

        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(result["results"][0]["error"], "boom")
        self.assertEqual(result["results"][0]["url"], "https://bad.example.com")
        self.assertNotIn("error", result["results"][1])

    def test_extract_error_field_in_payload_becomes_error_item(self):
        with mock.patch("search.make_request", return_value={"error": "Not enough credits"}):
            result = search.extract_serper(["https://example.com"], "k")
        self.assertEqual(result["results"][0]["error"], "Not enough credits")

    def test_extract_plus_uses_scrape_url_override_from_config(self):
        captured = {}

        def fake_post(url, headers, body, timeout=30):
            captured["url"] = url
            captured["timeout"] = timeout
            return _FAKE_SCRAPE_RESPONSE

        config = {"serper": {"scrape_url": "http://localhost:9200/scrape", "extract_timeout": 7}}
        with mock.patch.dict(os.environ, {"SERPER_API_KEY": "serper-test-key"}, clear=True):
            with mock.patch("search.make_request", side_effect=fake_post):
                result = search.extract_plus(["https://example.com"], provider="serper", config=config)

        self.assertEqual(result["provider"], "serper")
        self.assertEqual(captured["url"], "http://localhost:9200/scrape")
        self.assertEqual(captured["timeout"], 7)

    def test_extract_plus_auto_places_serper_last(self):
        self.assertEqual(search.EXTRACT_PROVIDER_PRIORITY[-1], "serper")

    def test_extract_plus_auto_falls_back_to_serper_when_only_serper_keyed(self):
        with mock.patch.dict(os.environ, {"SERPER_API_KEY": "serper-test-key"}, clear=True):
            with mock.patch("search.make_request", return_value=_FAKE_SCRAPE_RESPONSE):
                result = search.extract_plus(["https://example.com"], provider="auto", config={})

        self.assertEqual(result["provider"], "serper")
        self.assertEqual(result["routing"]["provider"], "serper")
        self.assertEqual(result["results"][0]["content"], "# Heading\n\nMarkdown body")


if __name__ == "__main__":
    unittest.main()
