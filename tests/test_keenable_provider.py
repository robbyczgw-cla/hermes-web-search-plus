import os
import unittest
from unittest import mock

import search
from config import get_api_key, validate_api_key
from provider_registry import KEENABLE_PUBLIC_SENTINEL


class KeenableKeyResolutionTests(unittest.TestCase):
    def test_get_api_key_returns_public_sentinel_when_no_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(get_api_key("keenable", {}), KEENABLE_PUBLIC_SENTINEL)

    def test_get_api_key_prefers_real_key(self):
        with mock.patch.dict(os.environ, {"KEENABLE_API_KEY": "keen_secret"}, clear=True):
            self.assertEqual(get_api_key("keenable", {}), "keen_secret")

    def test_validate_api_key_accepts_keyless_sentinel(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(validate_api_key("keenable", {}), KEENABLE_PUBLIC_SENTINEL)


class KeenableSearchTests(unittest.TestCase):
    def test_keyless_uses_public_endpoint_and_maps_results(self):
        fake_response = {
            "results": [{
                "title": "Keenable Result",
                "url": "https://example.com/keenable",
                "description": "Description text",
                "snippet": "Snippet text",
                "published_at": "2026-01-01",
                "acquired_at": "2026-01-02",
            }]
        }
        with mock.patch("search.make_request", return_value=fake_response) as mock_request:
            result = search.search_keenable(
                query="rust async patterns",
                api_key=KEENABLE_PUBLIC_SENTINEL,
                max_results=3,
                time_range="week",
                include_domains=["example.com"],
            )

        self.assertEqual(result["provider"], "keenable")
        self.assertEqual(result["results"][0]["snippet"], "Snippet text")
        self.assertEqual(result["results"][0]["url"], "https://example.com/keenable")

        url, headers, body = mock_request.call_args.args[:3]
        self.assertEqual(url, "https://api.keenable.ai/v1/search/public")
        self.assertNotIn("X-API-Key", headers)
        self.assertEqual(headers["X-Keenable-Title"], "hermes-web-search-plus")
        self.assertEqual(body["published_after"], "7d")
        self.assertEqual(body["site"], "example.com")

    def test_keyed_uses_authenticated_endpoint_with_description_fallback(self):
        fake_response = {"results": [{"title": "Keyed", "url": "https://example.com/keyed", "description": "Only description"}]}
        with mock.patch("search.make_request", return_value=fake_response) as mock_request:
            result = search.search_keenable(query="query", api_key="keen_secret", max_results=5)

        self.assertEqual(result["results"][0]["snippet"], "Only description")
        url, headers, _body = mock_request.call_args.args[:3]
        self.assertEqual(url, "https://api.keenable.ai/v1/search")
        self.assertEqual(headers["X-API-Key"], "keen_secret")


class KeenableExtractTests(unittest.TestCase):
    def test_keyless_fetches_via_public_endpoint(self):
        fake_response = {"url": "https://example.com", "title": "Example", "content": "# Page\nbody"}
        with mock.patch("search.make_get_request", return_value=fake_response) as mock_get:
            result = search.extract_keenable(["https://example.com"], KEENABLE_PUBLIC_SENTINEL)

        self.assertEqual(result["provider"], "keenable")
        self.assertEqual(result["results"][0]["content"], "# Page\nbody")
        self.assertEqual(result["results"][0]["title"], "Example")

        url, headers = mock_get.call_args.args[:2]
        self.assertTrue(url.startswith("https://api.keenable.ai/v1/fetch/public?url="))
        self.assertNotIn("X-API-Key", headers)
        self.assertEqual(headers["X-Keenable-Title"], "hermes-web-search-plus")

    def test_keyed_uses_authenticated_endpoint_and_header(self):
        fake_response = {"url": "https://example.com", "title": "Example", "content": "body"}
        with mock.patch("search.make_get_request", return_value=fake_response) as mock_get:
            search.extract_keenable(["https://example.com"], "keen_secret")

        url, headers = mock_get.call_args.args[:2]
        self.assertTrue(url.startswith("https://api.keenable.ai/v1/fetch?url="))
        self.assertEqual(headers["X-API-Key"], "keen_secret")

    def test_extract_plus_falls_back_to_keyless_keenable_when_no_key(self):
        fake_response = {"url": "https://example.com", "title": "Example", "content": "keenable body"}
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("search.make_get_request", return_value=fake_response):
                result = search.extract_plus(["https://example.com"], provider="auto", config={})

        self.assertEqual(result["provider"], "keenable")
        self.assertEqual(result["results"][0]["content"], "keenable body")
        self.assertEqual(result["routing"]["provider"], "keenable")


if __name__ == "__main__":
    unittest.main()
