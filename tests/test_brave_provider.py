from wsp_core import config as config_module
import os
import unittest
from unittest import mock

from wsp_core import providers
from wsp_core.search import auto_route_provider, get_api_key, validate_api_key


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
        with mock.patch("wsp_core.providers.make_get_request", return_value=fake_response):
            result = providers.search_brave(
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

    def test_auto_router_routes_to_brave_when_configured(self):
        config = config_module._deepcopy_default_config()
        config["auto_routing"]["provider_priority"] = ["serper", "tavily", "querit", "exa", "you", "searxng", "brave"]
        env = {
            "BRAVE_API_KEY": "brave-test-key-12345",
            "SERPER_API_KEY": "serper-test-key-12345",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            routing = auto_route_provider("weather in singapore today", config)

        # Brave leads the measured order, whatever the user's fallback order says.
        self.assertEqual(routing["provider"], "brave")
        self.assertEqual(routing["candidate_order"], ["brave", "serper"])


if __name__ == "__main__":
    unittest.main()
