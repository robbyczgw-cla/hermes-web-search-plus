"""Unified freshness parameter coverage.

Locks down the day|week|month|year freshness contract: the central mapping
table matches what each provider function actually sends, invalid values fail
with the standard error dict, the value is passed down into provider requests,
and unsupported providers still run while reporting applied=false in metadata.
"""

import contextlib
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


class FreshnessMappingTests(unittest.TestCase):
    def test_mapping_table_per_provider(self):
        expected = {
            "serper": {"day": "qdr:d", "week": "qdr:w", "month": "qdr:m", "year": "qdr:y"},
            "brave": {"day": "pd", "week": "pw", "month": "pm", "year": "py"},
            "querit": {"day": "d1", "week": "w1", "month": "m1", "year": "y1"},
            "firecrawl": {"day": "qdr:d", "week": "qdr:w", "month": "qdr:m", "year": "qdr:y"},
            "keenable": {"day": "1d", "week": "7d", "month": "1mo", "year": "1y"},
            "you": {"day": "day", "week": "week", "month": "month", "year": "year"},
            "perplexity": {"day": "day", "week": "week", "month": "month", "year": "year"},
            "kilo-perplexity": {"day": "day", "week": "week", "month": "month", "year": "year"},
            "searxng": {"day": "day", "week": "week", "month": "month", "year": "year"},
        }
        self.assertEqual(providers.PROVIDER_FRESHNESS_FORMATS, expected)

    def test_map_freshness_for_provider(self):
        self.assertEqual(providers.map_freshness_for_provider("brave", "week"), "pw")
        self.assertEqual(providers.map_freshness_for_provider("serper", "day"), "qdr:d")
        self.assertIsNone(providers.map_freshness_for_provider("tavily", "week"))
        self.assertIsNone(providers.map_freshness_for_provider("brave", None))

    def test_unsupported_providers_are_not_in_table(self):
        for provider in ("tavily", "exa", "linkup", "parallel", "serpbase"):
            self.assertFalse(providers.provider_supports_freshness(provider), provider)

    def test_table_matches_existing_provider_mappers(self):
        # The central table must never drift from the maps the provider
        # functions actually use when building requests.
        for generic, native in providers.PROVIDER_FRESHNESS_FORMATS["keenable"].items():
            self.assertEqual(providers._KEENABLE_TIME_RANGE[generic], native)
        for generic, native in providers.PROVIDER_FRESHNESS_FORMATS["firecrawl"].items():
            self.assertEqual(providers._map_firecrawl_time_range(generic), native)
        for generic, native in providers.PROVIDER_FRESHNESS_FORMATS["querit"].items():
            self.assertEqual(providers._map_querit_time_range(generic), native)

    def test_freshness_metadata_shapes(self):
        applied = providers.freshness_metadata("brave", "week")
        self.assertEqual(applied, {
            "requested": "week",
            "applied": True,
            "provider": "brave",
            "native_value": "pw",
        })
        skipped = providers.freshness_metadata("tavily", "week")
        self.assertEqual(skipped, {
            "requested": "week",
            "applied": False,
            "provider": "tavily",
            "reason": "provider tavily does not support freshness",
        })


class NormalizeFreshnessTests(unittest.TestCase):
    def test_valid_values_pass_through(self):
        for value in providers.FRESHNESS_VALUES:
            self.assertEqual(providers.normalize_freshness(value), value)

    def test_case_insensitive_and_trimmed(self):
        self.assertEqual(providers.normalize_freshness("WEEK"), "week")
        self.assertEqual(providers.normalize_freshness(" Day "), "day")

    def test_unset_values_return_none(self):
        self.assertIsNone(providers.normalize_freshness(None))
        self.assertIsNone(providers.normalize_freshness(""))
        self.assertIsNone(providers.normalize_freshness("   "))

    def test_invalid_value_raises_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            providers.normalize_freshness("fortnight")
        message = str(ctx.exception)
        self.assertIn("fortnight", message)
        self.assertIn("day, week, month, year", message)


class FreshnessRequestTests(unittest.TestCase):
    def test_brave_request_includes_native_freshness(self):
        captured = {}

        def fake_get(url, headers, timeout=30):
            captured["url"] = url
            return {"web": {"results": []}}

        with mock.patch("search.make_get_request", side_effect=fake_get):
            search.search_brave(query="q", api_key="brave-key", time_range="week")

        self.assertIn("freshness=pw", captured["url"])

    def test_serper_request_includes_tbs(self):
        captured = {}

        def fake_post(url, headers, body, timeout=30):
            captured["body"] = body
            return {"organic": []}

        with mock.patch("search.make_request", side_effect=fake_post):
            search.search_serper(query="q", api_key="serper-key", time_range="month")

        self.assertEqual(captured["body"]["tbs"], "qdr:m")

    def test_you_request_includes_freshness_param(self):
        captured = {}

        def fake_urlopen(req, timeout=30):
            captured["url"] = req.full_url

            class _Resp:
                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

            return _Resp()

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch("urllib.request.urlopen", side_effect=fake_urlopen))
            stack.enter_context(mock.patch.object(
                providers, "_read_json_response",
                return_value={"results": {"web": [], "news": []}, "metadata": {}},
            ))
            search.search_you(query="q", api_key="you-key", freshness="day")

        self.assertIn("freshness=day", captured["url"])

    def test_perplexity_request_includes_recency_filter(self):
        captured = {}

        def fake_post(url, headers, body, timeout=30):
            captured["body"] = body
            return {"choices": [{"message": {"content": "answer"}}], "citations": []}

        with mock.patch("search.make_request", side_effect=fake_post):
            search.search_perplexity(query="q", api_key="pplx-key", freshness="year")

        self.assertEqual(captured["body"]["search_recency_filter"], "year")


class FreshnessPipelineTests(unittest.TestCase):
    def _isolate(self, stack):
        # Keep the pipeline off the real cache/cooldown/health files.
        stack.enter_context(mock.patch.object(search, "provider_in_cooldown", lambda p: (False, 0)))
        stack.enter_context(mock.patch.object(search, "cache_get", lambda **kw: None))
        stack.enter_context(mock.patch.object(search, "cache_put", lambda **kw: None))
        stack.enter_context(mock.patch.object(search, "reset_provider_health", lambda p: None))

    def test_invalid_freshness_returns_error_dict(self):
        result = search.run_search_request(query="q", provider="serper", freshness="fortnight")
        self.assertIn("Invalid freshness value", result["error"])
        self.assertEqual(result["provider"], "serper")
        self.assertEqual(result["results"], [])

    def test_freshness_reaches_provider_and_metadata_reports_applied(self):
        with contextlib.ExitStack() as stack:
            self._isolate(stack)
            stack.enter_context(mock.patch.dict("os.environ", {"SERPER_API_KEY": "serper-test-key"}))
            seen = {}

            def fake_serper(**kwargs):
                seen.update(kwargs)
                return _canned("serper")

            stack.enter_context(mock.patch.object(search, "search_serper", fake_serper))
            result = search.run_search_request(query="latest news", provider="serper", freshness="WEEK")

        self.assertEqual(seen["time_range"], "week")
        self.assertEqual(result["metadata"]["freshness"], {
            "requested": "week",
            "applied": True,
            "provider": "serper",
            "native_value": "qdr:w",
        })

    def test_unsupported_provider_still_searches_and_reports_not_applied(self):
        with contextlib.ExitStack() as stack:
            self._isolate(stack)
            stack.enter_context(mock.patch.dict("os.environ", {"TAVILY_API_KEY": "tavily-test-key"}))
            stack.enter_context(mock.patch.object(search, "search_tavily", lambda **kw: _canned("tavily")))
            result = search.run_search_request(query="how does https work", provider="tavily", freshness="week")

        self.assertEqual(result["results"][0]["url"], "https://example.test/a")
        self.assertEqual(result["metadata"]["freshness"], {
            "requested": "week",
            "applied": False,
            "provider": "tavily",
            "reason": "provider tavily does not support freshness",
        })

    def test_research_mode_reports_freshness_per_provider(self):
        with contextlib.ExitStack() as stack:
            self._isolate(stack)
            stack.enter_context(mock.patch.dict("os.environ", {
                "SERPER_API_KEY": "serper-test-key",
                "TAVILY_API_KEY": "tavily-test-key",
            }))
            seen = {}

            def fake_serper(**kwargs):
                seen.update(kwargs)
                return _canned("serper")

            stack.enter_context(mock.patch.object(search, "search_serper", fake_serper))
            stack.enter_context(mock.patch.object(search, "search_tavily", lambda **kw: _canned("tavily")))
            stack.enter_context(mock.patch.object(
                search, "extract_plus", lambda **kw: {"provider": None, "results": []},
            ))

            parser = search.build_parser({})
            args = parser.parse_args([
                "--query", "compare turntables",
                "--provider", "serper",
                "--mode", "research",
                "--research-providers", "serper", "tavily",
                "--freshness", "week",
            ])
            payload, exit_code = search.execute_search_request(args, {})

        self.assertEqual(exit_code, 0)
        self.assertEqual(seen["time_range"], "week")
        by_provider = {m["provider"]: m for m in payload["metadata"]["freshness"]["providers"]}
        self.assertEqual(payload["metadata"]["freshness"]["requested"], "week")
        self.assertTrue(by_provider["serper"]["applied"])
        self.assertFalse(by_provider["tavily"]["applied"])
        self.assertIn("does not support freshness", by_provider["tavily"]["reason"])

    def test_cli_parser_lowercases_freshness(self):
        parser = search.build_parser({})
        args = parser.parse_args(["--query", "q", "--freshness", "WEEK"])
        self.assertEqual(args.freshness, "week")


if __name__ == "__main__":
    unittest.main()
