import os
import time
import unittest
from unittest import mock

import providers
import search
from search import get_api_key, validate_api_key


class LinkupProviderTests(unittest.TestCase):
    def test_get_api_key_reads_linkup_env(self):
        with mock.patch.dict(os.environ, {"LINKUP_API_KEY": "linkup-test-key"}, clear=False):
            self.assertEqual(get_api_key("linkup", {}), "linkup-test-key")

    def test_validate_api_key_accepts_linkup(self):
        with mock.patch.dict(os.environ, {"LINKUP_API_KEY": "linkup-test-key-12345"}, clear=False):
            self.assertEqual(validate_api_key("linkup", {}), "linkup-test-key-12345")

    def test_search_linkup_parses_search_results(self):
        fake_response = {
            "results": [
                {
                    "name": "Evidence source",
                    "url": "https://example.com/evidence",
                    "content": "Useful evidence snippet",
                    "type": "text",
                    "favicon": "https://example.com/favicon.ico",
                }
            ]
        }
        with mock.patch("search.make_request", return_value=fake_response) as mock_request:
            result = search.search_linkup(
                query="find credible sources for AI tutoring outcomes",
                api_key="linkup-test-key-12345",
                max_results=5,
                depth="standard",
                output_type="searchResults",
                include_domains=["example.com"],
                exclude_domains=["wikipedia.org"],
            )

        self.assertEqual(result["provider"], "linkup")
        self.assertEqual(result["results"][0]["title"], "Evidence source")
        self.assertEqual(result["results"][0]["url"], "https://example.com/evidence")
        self.assertEqual(result["results"][0]["snippet"], "Useful evidence snippet")
        self.assertEqual(result["results"][0]["type"], "text")

        _, headers, body = mock_request.call_args.args[:3]
        self.assertEqual(headers["Authorization"], "Bearer linkup-test-key-12345")
        self.assertEqual(body["q"], "find credible sources for AI tutoring outcomes")
        self.assertEqual(body["depth"], "standard")
        self.assertEqual(body["outputType"], "searchResults")
        self.assertEqual(body["includeDomains"], ["example.com"])
        self.assertEqual(body["excludeDomains"], ["wikipedia.org"])

    def test_search_linkup_parses_sourced_answer(self):
        fake_response = {
            "answer": "The claim is supported by current studies.",
            "sources": [
                {
                    "name": "Study source",
                    "url": "https://example.org/study",
                    "snippet": "A relevant study snippet",
                }
            ],
        }
        with mock.patch("search.make_request", return_value=fake_response):
            result = search.search_linkup(
                query="fact check AI tutoring outcomes with citations",
                api_key="linkup-test-key-12345",
                output_type="sourcedAnswer",
            )

        self.assertEqual(result["answer"], "The claim is supported by current studies.")
        self.assertEqual(result["results"][0]["title"], "Study source")
        self.assertEqual(result["results"][0]["snippet"], "A relevant study snippet")

    def test_extract_linkup_batch_keeps_partial_results_when_one_fetch_hangs(self):
        def fake_make_request(url, headers, body, timeout=30):
            if body["url"] == "https://slow.test/page":
                time.sleep(1.0)
            return {"markdown": f"content for {body['url']}"}

        start = time.monotonic()
        with mock.patch.object(providers, "make_request", fake_make_request):
            with mock.patch.object(providers, "_BATCH_TIMEOUT_GRACE_SECONDS", 0.3):
                result = providers.extract_linkup(
                    ["https://fast.test/page", "https://slow.test/page"],
                    api_key="linkup-test-key-12345",
                    timeout=0,
                )
        elapsed = time.monotonic() - start

        # The batch must return within the bounded window with partial results
        # instead of blocking on the hung fetch.
        self.assertLess(elapsed, 0.9)
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(result["results"][0]["url"], "https://fast.test/page")
        self.assertFalse(result["results"][0].get("error"))
        self.assertEqual(result["results"][1]["url"], "https://slow.test/page")
        self.assertIn("timed out", result["results"][1]["error"])

    def test_auto_router_prefers_linkup_for_source_grounding_queries(self):
        config = {
            "auto_routing": {"provider_priority": ["linkup", "tavily", "exa", "serper"]},
        }
        with mock.patch.dict(os.environ, {"LINKUP_API_KEY": "linkup-test-key"}, clear=False):
            routing = search.auto_route_provider(
                "find credible sources and citations to verify this claim",
                config,
            )

        self.assertEqual(routing["provider"], "linkup")
        self.assertGreater(routing["scores"]["linkup"], routing["scores"].get("tavily", 0))


if __name__ == "__main__":
    unittest.main()
