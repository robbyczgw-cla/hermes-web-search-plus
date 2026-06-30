import json
import os
import tempfile
import unittest
from unittest import mock

import __init__ as plugin
import providers
import search
from config import (
    ProviderConfigError,
    get_api_key,
    keyless_public_allowed,
    provider_configured,
    validate_api_key,
)
from env_loader import is_truthy


def _allow_public_config():
    return {"keenable": {"allow_public": True}}


class KeenableKeyResolutionTests(unittest.TestCase):
    def test_get_api_key_returns_none_when_no_key(self):
        """Keyless must not be truthy as a key, so key-status logic never treats it as keyed."""
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(get_api_key("keenable", {}))
            self.assertIsNone(get_api_key("keenable", _allow_public_config()))

    def test_get_api_key_prefers_real_key(self):
        with mock.patch.dict(os.environ, {"KEENABLE_API_KEY": "keen_secret"}, clear=True):
            self.assertEqual(get_api_key("keenable", {}), "keen_secret")

    def test_keyless_not_treated_as_key_even_when_opted_in(self):
        """Opting in must not make key-status truthy, yet provider_configured is True."""
        with mock.patch.dict(os.environ, {}, clear=True):
            cfg = _allow_public_config()
            self.assertFalse(bool(get_api_key("keenable", cfg)))
            self.assertTrue(provider_configured("keenable", cfg))
            self.assertFalse(provider_configured("keenable", {}))

    def test_keyless_public_allowed_is_opt_in(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(keyless_public_allowed("keenable", {}))
            self.assertTrue(keyless_public_allowed("keenable", _allow_public_config()))
            self.assertFalse(keyless_public_allowed("serper", _allow_public_config()))

    def test_keyless_public_allowed_via_env(self):
        with mock.patch.dict(os.environ, {"KEENABLE_ALLOW_PUBLIC": "1"}, clear=True):
            self.assertTrue(keyless_public_allowed("keenable", {}))

    def test_validate_api_key_requires_opt_in_for_keyless(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ProviderConfigError):
                validate_api_key("keenable", {})
            self.assertIsNone(validate_api_key("keenable", _allow_public_config()))

    def test_validate_api_key_prefers_real_key(self):
        with mock.patch.dict(os.environ, {"KEENABLE_API_KEY": "keen_secret_value"}, clear=True):
            self.assertEqual(validate_api_key("keenable", {}), "keen_secret_value")


class KeenablePublicOptInStrictnessTests(unittest.TestCase):
    TRUE_VALUES = ["1", "true", "TRUE", "yes", "On"]
    FALSE_VALUES = ["0", "false", "no", "off", "", "2", "enabled"]

    def test_is_truthy_helper(self):
        for v in ["1", "true", "True", "YES", "on", True, 1]:
            self.assertTrue(is_truthy(v), v)
        for v in ["0", "false", "no", "off", "", None, False, 0, "2", "tru"]:
            self.assertFalse(is_truthy(v), v)

    def test_env_opt_in_is_strict(self):
        for v in self.TRUE_VALUES:
            with mock.patch.dict(os.environ, {"KEENABLE_ALLOW_PUBLIC": v}, clear=True):
                self.assertTrue(keyless_public_allowed("keenable", {}), f"env={v!r}")
        for v in self.FALSE_VALUES:
            with mock.patch.dict(os.environ, {"KEENABLE_ALLOW_PUBLIC": v}, clear=True):
                self.assertFalse(keyless_public_allowed("keenable", {}), f"env={v!r}")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(keyless_public_allowed("keenable", {}))

    def test_config_opt_in_is_strict(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            for v in [True, "true", "yes", "on", "1", 1]:
                self.assertTrue(keyless_public_allowed("keenable", {"keenable": {"allow_public": v}}), f"cfg={v!r}")
            for v in [False, "false", "no", "off", "0", 0, ""]:
                self.assertFalse(keyless_public_allowed("keenable", {"keenable": {"allow_public": v}}), f"cfg={v!r}")

    def test_registration_env_opt_in_is_strict(self):
        base = {"WEB_SEARCH_PLUS_CONFIG": "/nonexistent-strict.json"}
        for v in self.TRUE_VALUES:
            with mock.patch.dict(os.environ, {**base, "KEENABLE_ALLOW_PUBLIC": v}, clear=True):
                self.assertTrue(plugin._keyless_public_opted_in("keenable"), f"env={v!r}")
        for v in self.FALSE_VALUES:
            with mock.patch.dict(os.environ, {**base, "KEENABLE_ALLOW_PUBLIC": v}, clear=True):
                self.assertFalse(plugin._keyless_public_opted_in("keenable"), f"env={v!r}")

    def test_registration_config_file_opt_in_is_strict(self):
        for v, expected in [(True, True), ("yes", True), ("false", False), (False, False), (0, False)]:
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
                json.dump({"keenable": {"allow_public": v}}, f)
                path = f.name
            try:
                with mock.patch.dict(os.environ, {"WEB_SEARCH_PLUS_CONFIG": path}, clear=True):
                    self.assertEqual(plugin._keyless_public_opted_in("keenable"), expected, f"cfg={v!r}")
            finally:
                os.unlink(path)


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
                public=True,
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


class KeenablePublicWarningTests(unittest.TestCase):
    def test_public_route_warns_once(self):
        with mock.patch.object(providers, "_KEENABLE_PUBLIC_WARNED", False):
            with mock.patch("providers.print") as mock_print:
                providers._keenable_endpoint("https://api.keenable.ai/v1/search", None, public=True)
                providers._keenable_endpoint("https://api.keenable.ai/v1/search", None, public=True)
        self.assertEqual(mock_print.call_count, 1)

    def test_keyed_route_does_not_warn(self):
        with mock.patch.object(providers, "_KEENABLE_PUBLIC_WARNED", False):
            with mock.patch("providers.print") as mock_print:
                providers._keenable_endpoint("https://api.keenable.ai/v1/search", "keen_secret", public=False)
        mock_print.assert_not_called()

    def test_key_wins_even_when_public_enabled(self):
        with mock.patch.object(providers, "_KEENABLE_PUBLIC_WARNED", False):
            with mock.patch("providers.print") as mock_print:
                url, headers = providers._keenable_endpoint("https://api.keenable.ai/v1/search", "keen_secret", public=True)
        self.assertEqual(url, "https://api.keenable.ai/v1/search")
        self.assertEqual(headers["X-API-Key"], "keen_secret")
        mock_print.assert_not_called()

    def test_raises_without_key_or_public(self):
        with self.assertRaises(ValueError):
            providers._keenable_endpoint("https://api.keenable.ai/v1/search", None, public=False)


class KeenableExtractTests(unittest.TestCase):
    def setUp(self):
        search.reset_provider_health("keenable")

    def tearDown(self):
        search.reset_provider_health("keenable")

    def test_keyless_fetches_via_public_endpoint(self):
        fake_response = {"url": "https://example.com", "title": "Example", "content": "# Page\nbody"}
        with mock.patch("search.make_get_request", return_value=fake_response) as mock_get:
            result = search.extract_keenable(["https://example.com"], public=True)

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

    def test_key_wins_over_public_when_both_set(self):
        fake_response = {"url": "https://example.com", "title": "X", "content": "body"}
        with mock.patch.dict(os.environ, {"KEENABLE_API_KEY": "keen_secret"}, clear=True):
            with mock.patch("search.make_get_request", return_value=fake_response) as mock_get:
                result = search.extract_plus(["https://example.com"], provider="keenable",
                                             config={"keenable": {"allow_public": True}})

        self.assertEqual(result["provider"], "keenable")
        url, headers = mock_get.call_args.args[:2]
        self.assertNotIn("/fetch/public", url)
        self.assertEqual(headers["X-API-Key"], "keen_secret")

    def test_extract_plus_skips_keenable_when_not_opted_in(self):
        """No key and no opt-in: keenable is not a silent fallback."""
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("search.make_get_request") as mock_get:
                result = search.extract_plus(["https://example.com"], provider="auto", config={})

        mock_get.assert_not_called()
        self.assertNotEqual(result.get("provider"), "keenable")
        self.assertTrue(result.get("error") or result.get("errors"))

    def test_extract_plus_falls_back_to_keenable_when_opted_in(self):
        fake_response = {"url": "https://example.com", "title": "Example", "content": "keenable body"}
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("search.make_get_request", return_value=fake_response):
                result = search.extract_plus(["https://example.com"], provider="auto", config=_allow_public_config())

        self.assertEqual(result["provider"], "keenable")
        self.assertEqual(result["results"][0]["content"], "keenable body")
        self.assertEqual(result["routing"]["provider"], "keenable")

    def test_explicit_search_missing_key_does_not_cooldown_keenable(self):
        """Config errors should not poison provider health for later keyless extract tests."""
        with mock.patch.dict(os.environ, {}, clear=True):
            result = search.run_search_request(query="config smoke", provider="keenable", config={})

        self.assertEqual(result["provider"], "keenable")
        self.assertEqual(result["error"], "All providers failed")
        self.assertEqual(search.provider_in_cooldown("keenable"), (False, 0))


if __name__ == "__main__":
    unittest.main()
