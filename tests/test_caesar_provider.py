import os
import unittest
from unittest import mock

import search
from config import (
    ProviderConfigError,
    get_api_key,
    provider_configured,
    validate_api_key,
)


class CaesarKeyResolutionTests(unittest.TestCase):
    def test_get_api_key_returns_none_when_no_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(get_api_key("caesar", {}))

    def test_get_api_key_reads_env(self):
        with mock.patch.dict(os.environ, {"CAESAR_API_KEY": "sk_live_secret"}, clear=True):
            self.assertEqual(get_api_key("caesar", {}), "sk_live_secret")

    def test_provider_configured_requires_a_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(provider_configured("caesar", {}))
        with mock.patch.dict(os.environ, {"CAESAR_API_KEY": "sk_live_secret"}, clear=True):
            self.assertTrue(provider_configured("caesar", {}))

    def test_validate_api_key_raises_without_a_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ProviderConfigError):
                validate_api_key("caesar", {})

    def test_validate_api_key_returns_the_key(self):
        with mock.patch.dict(os.environ, {"CAESAR_API_KEY": "sk_live_secret_value"}, clear=True):
            self.assertEqual(validate_api_key("caesar", {}), "sk_live_secret_value")


class CaesarSearchTests(unittest.TestCase):
    def test_sends_bearer_header_and_maps_results(self):
        fake_response = {
            "results": [{
                "title": "Caesar Result",
                "url": "https://example.com/caesar",
                "snippet": "Snippet text",
                "score": 0.91,
                "metadata": {"published_at": "2026-06-01T00:00:00Z"},
            }],
            "access": {"tier": "standard"},
        }
        with mock.patch("search.make_request", return_value=fake_response) as mock_request:
            result = search.search_caesar(
                query="rust async patterns", api_key="sk_live_secret", max_results=3
            )

        self.assertEqual(result["provider"], "caesar")
        self.assertEqual(result["results"][0]["snippet"], "Snippet text")
        self.assertEqual(result["results"][0]["url"], "https://example.com/caesar")
        self.assertEqual(result["results"][0]["score"], 0.91)
        self.assertEqual(result["results"][0]["date"], "2026-06-01T00:00:00Z")
        self.assertEqual(result["metadata"]["tier"], "standard")

        url, headers, body = mock_request.call_args.args[:3]
        self.assertEqual(url, "https://alpha.api.trycaesar.com/v1/search")
        self.assertEqual(headers["Authorization"], "Bearer sk_live_secret")
        self.assertEqual(body, {"query": "rust async patterns", "max_results": 3})

    def test_prefers_query_selected_passages_over_meta_description(self):
        fake_response = {
            "results": [{
                "title": "Tokio scheduler",
                "canonical_url": "https://tokio.rs/blog/2019-10-scheduler",
                # Caesar's `snippet` is the page's meta description, identical for
                # every query that surfaces this document.
                "snippet": "A blog about the Tokio runtime.",
                "passages": [
                    {"text": "Work stealing balances load across worker threads."},
                    {"text": "The new scheduler avoids the atomic increment in wake_by_ref."},
                ],
                "metadata": {"published_at": "2019-10-13T00:00:00Z"},
            }]
        }
        with mock.patch("search.make_request", return_value=fake_response):
            result = search.search_caesar(query="work stealing", api_key="sk_live_secret")

        first = result["results"][0]
        self.assertEqual(
            first["snippet"],
            "Work stealing balances load across worker threads.\n\n"
            "The new scheduler avoids the atomic increment in wake_by_ref.",
        )
        self.assertEqual(first["date"], "2019-10-13T00:00:00Z")
        # The synthesized answer draws on the passages, not the meta description.
        self.assertIn("Work stealing", result["answer"])

    def test_falls_back_to_snippet_when_no_passages(self):
        fake_response = {"results": [{"title": "A", "url": "https://a.test", "snippet": "only a snippet"}]}
        with mock.patch("search.make_request", return_value=fake_response):
            result = search.search_caesar(query="q", api_key="sk_live_secret")

        self.assertEqual(result["results"][0]["snippet"], "only a snippet")
        self.assertNotIn("date", result["results"][0])

    def test_ignores_blank_passages_and_tolerates_string_passages(self):
        fake_response = {
            "results": [
                {"url": "https://a.test", "snippet": "fallback", "passages": [{"text": "   "}]},
                {"url": "https://b.test", "passages": ["a plain string passage"]},
            ]
        }
        with mock.patch("search.make_request", return_value=fake_response):
            result = search.search_caesar(query="q", api_key="sk_live_secret")

        self.assertEqual(result["results"][0]["snippet"], "fallback")
        self.assertEqual(result["results"][1]["snippet"], "a plain string passage")

    def test_tolerates_object_and_null_scores(self):
        fake_response = {
            "results": [
                {"url": "https://a.example", "snippet": "a", "score": {"value": 0.8}},
                {"url": "https://b.example", "snippet": "b", "score": None},
            ]
        }
        with mock.patch("search.make_request", return_value=fake_response):
            result = search.search_caesar(query="q", api_key="sk_live_secret")

        # Object-form score is unwrapped; a null score falls back to a rank-derived value.
        self.assertEqual(result["results"][0]["score"], 0.8)
        self.assertIsInstance(result["results"][1]["score"], float)

    def test_url_and_snippet_field_fallbacks(self):
        fake_response = {
            "results": [{"canonical_url": "https://c.example", "content": "body text"}]
        }
        with mock.patch("search.make_request", return_value=fake_response):
            result = search.search_caesar(query="q", api_key="sk_live_secret")

        self.assertEqual(result["results"][0]["url"], "https://c.example")
        self.assertEqual(result["results"][0]["snippet"], "body text")


if __name__ == "__main__":
    unittest.main()
