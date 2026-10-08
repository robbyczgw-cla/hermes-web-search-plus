import os
import unittest
from unittest import mock

import search
from search import QueryAnalyzer, get_api_key, validate_api_key


class BraveProviderTests(unittest.TestCase):
    def test_get_api_key_reads_brave_env(self):
        with mock.patch.dict(os.environ, {"BRAVE_API_KEY": "brave-test-key"}, clear=False):
            self.assertEqual(get_api_key("brave", {}), "brave-test-key")

    def test_validate_api_key_accepts_brave(self):
        with mock.patch.dict(os.environ, {"BRAVE_API_KEY": "brave-test-key-12345"}, clear=False):
            self.assertEqual(validate_api_key("brave", {}), "brave-test-key-12345")

    def test_search_brave_parses_web_results(self):
        fake_response = {
            "web": {
                "results": [
                    {
                        "title": "Example result",
                        "url": "https://example.com/page",
                        "description": "Example snippet",
                        "age": "2 days ago",
                    }
                ]
            }
        }
        with mock.patch("search.make_get_request", return_value=fake_response):
            result = search.search_brave(
                query="example query",
                api_key="brave-test-key-12345",
                max_results=5,
                country="us",
                language="en",
                time_range="week",
            )

        self.assertEqual(result["provider"], "brave")
        self.assertEqual(result["results"][0]["title"], "Example result")
        self.assertEqual(result["results"][0]["url"], "https://example.com/page")
        self.assertEqual(result["results"][0]["snippet"], "Example snippet")

    def test_query_analyzer_can_route_to_brave(self):
        config = search.DEFAULT_CONFIG.copy()
        config["auto_routing"] = dict(search.DEFAULT_CONFIG["auto_routing"])
        config["auto_routing"]["provider_priority"] = ["brave", "serper", "tavily", "querit", "exa", "you", "searxng"]
        analyzer = QueryAnalyzer(config)

        env = {
            "BRAVE_API_KEY": "brave-test-key-12345",
            "SERPER_API_KEY": "serper-test-key-12345",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            routing = analyzer.route("weather in singapore today")

        self.assertIn(routing["provider"], {"brave", "serper"})


if __name__ == "__main__":
    unittest.main()
