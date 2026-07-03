"""Configurable search locale defaults and query language inference coverage.

Locks down the locale contract: country is config-first (CLI flag > explicit
provider config > query location hint > defaults.locale.country > "us"),
language is query-aware (CLI flag > explicit provider config >
defaults.locale.language with "auto" inference > "en"), query language never
implies a country, resolved values reach the provider requests, and result
metadata reports where each value came from. All provider calls are mocked;
no network access.
"""

import contextlib
import unittest
from unittest import mock

import routing
import search
import search_locale
from search_locale import detect_location_country, provider_supports_locale, resolve_locale


def _config(locale=None, **provider_overrides):
    """Build a merged runtime config like load_config would produce."""
    config = search._deepcopy_default_config()
    if locale is not None:
        config["defaults"]["locale"] = locale
    for provider, section in provider_overrides.items():
        config.setdefault(provider, {}).update(section)
    return config


class LanguageInferenceTests(unittest.TestCase):
    def test_german_query_is_inferred(self):
        self.assertEqual(routing.infer_query_language("wie funktioniert eine Wärmepumpe im Winter"), "de")

    def test_spanish_query_is_inferred(self):
        self.assertEqual(routing.infer_query_language("mejores restaurantes veganos cerca del centro"), "es")

    def test_french_query_is_inferred(self):
        self.assertEqual(routing.infer_query_language("les meilleures boulangeries avec horaires"), "fr")

    def test_english_query_is_inferred(self):
        self.assertEqual(routing.infer_query_language("what are the best coffee houses with long opening hours"), "en")

    def test_short_technical_query_infers_nothing(self):
        self.assertIsNone(routing.infer_query_language("DAC R2R NOS"))
        self.assertIsNone(routing.infer_query_language("PostgreSQL 17 release notes"))

    def test_single_shared_stopword_is_below_threshold(self):
        # "que" exists in es/fr/pt: one ambiguous signal must not infer anything.
        self.assertIsNone(routing.infer_query_language("que"))

    def test_empty_query_infers_nothing(self):
        self.assertIsNone(routing.infer_query_language(""))
        self.assertIsNone(routing.infer_query_language(None))

    def test_min_matches_is_a_named_constant(self):
        self.assertGreaterEqual(routing.LANGUAGE_INFERENCE_MIN_MATCHES, 2)


class LocationHintTests(unittest.TestCase):
    def test_known_city_hints_map_to_countries(self):
        self.assertEqual(detect_location_country("mejores restaurantes Madrid"), "es")
        self.assertEqual(detect_location_country("boulangerie Paris horaires"), "fr")
        self.assertEqual(detect_location_country("beste Kaffeehäuser in Wien"), "at")
        self.assertEqual(detect_location_country("coworking spaces in Berlin"), "de")
        self.assertEqual(detect_location_country("museums in London"), "gb")

    def test_substring_of_longer_word_does_not_hint(self):
        # "Wiener" contains "wien" but is not an explicit location token.
        self.assertIsNone(detect_location_country("Wiener Melange Rezept"))

    def test_conflicting_hints_resolve_to_none(self):
        self.assertIsNone(detect_location_country("compare bakeries in Paris and Madrid"))

    def test_no_hint_returns_none(self):
        self.assertIsNone(detect_location_country("how does HTTPS encryption work"))
        self.assertIsNone(detect_location_country(""))
        self.assertIsNone(detect_location_country(None))


class ResolveLocaleTests(unittest.TestCase):
    def test_defaults_stay_us_en(self):
        country, language, meta = resolve_locale("serper", _config(), "PostgreSQL 17 release notes")
        self.assertEqual((country, language), ("us", "en"))
        self.assertEqual(meta["source"], {"country": "fallback", "language": "fallback"})

    def test_missing_locale_section_stays_us_en(self):
        config = _config()
        config["defaults"].pop("locale")
        country, language, _ = resolve_locale("serper", config, "PostgreSQL 17 release notes")
        self.assertEqual((country, language), ("us", "en"))

    def test_configured_country_with_auto_language_and_german_query(self):
        config = _config(locale={"country": "at", "language": "auto"})
        country, language, meta = resolve_locale(
            "serper", config, "wie funktioniert eine Wärmepumpe im Winter"
        )
        self.assertEqual((country, language), ("at", "de"))
        self.assertEqual(meta["source"], {"country": "config", "language": "inferred"})

    def test_english_query_keeps_configured_country(self):
        config = _config(locale={"country": "at", "language": "auto"})
        country, language, _ = resolve_locale(
            "serper", config, "what are the best coffee houses with long opening hours"
        )
        self.assertEqual((country, language), ("at", "en"))

    def test_location_hint_overrides_configured_country(self):
        config = _config(locale={"country": "at", "language": "auto"})
        country, language, meta = resolve_locale("serper", config, "mejores restaurantes veganos Madrid")
        self.assertEqual((country, language), ("es", "es"))
        self.assertEqual(meta["source"], {"country": "hint", "language": "inferred"})

    def test_query_language_never_implies_country(self):
        # A German query without an explicit location hint must keep the
        # configured country (could be Austria or Switzerland, not Germany).
        config = _config(locale={"country": "at", "language": "auto"})
        country, _, meta = resolve_locale("serper", config, "wie funktioniert eine Wärmepumpe im Winter")
        self.assertEqual(country, "at")
        self.assertNotEqual(country, "de")
        self.assertEqual(meta["source"]["country"], "config")

    def test_short_technical_query_falls_back_to_default_language(self):
        config = _config(locale={"country": "at", "language": "auto"})
        country, language, meta = resolve_locale("serper", config, "DAC R2R NOS")
        self.assertEqual((country, language), ("at", "en"))
        self.assertEqual(meta["source"]["language"], "fallback")

    def test_concrete_default_language_disables_inference(self):
        config = _config(locale={"country": "at", "language": "de"})
        _, language, meta = resolve_locale(
            "serper", config, "what are the best coffee houses with long opening hours"
        )
        self.assertEqual(language, "de")
        self.assertEqual(meta["source"]["language"], "config")

    def test_cli_flags_beat_everything(self):
        config = _config(
            locale={"country": "at", "language": "auto"},
            serper={"country": "gb", "language": "en"},
        )
        country, language, meta = resolve_locale(
            "serper", config, "mejores restaurantes veganos Madrid",
            cli_country="FR", cli_language="FR",
        )
        self.assertEqual((country, language), ("fr", "fr"))
        self.assertEqual(meta["source"], {"country": "cli", "language": "cli"})

    def test_explicit_provider_config_beats_hint_and_global_defaults(self):
        config = _config(
            locale={"country": "at", "language": "auto"},
            serper={"country": "gb", "language": "en"},
        )
        country, language, meta = resolve_locale("serper", config, "mejores restaurantes veganos Madrid")
        self.assertEqual((country, language), ("gb", "en"))
        self.assertEqual(meta["source"], {"country": "config", "language": "config"})

    def test_brave_reads_its_search_lang_key(self):
        config = _config(locale={"country": "at", "language": "auto"}, brave={"search_lang": "fr"})
        _, language, meta = resolve_locale("brave", config, "wie funktioniert eine Wärmepumpe")
        self.assertEqual(language, "fr")
        self.assertEqual(meta["source"]["language"], "config")

    def test_locale_capability_table(self):
        for provider in ("serper", "serpbase", "brave", "querit", "firecrawl", "you", "searxng"):
            self.assertTrue(provider_supports_locale(provider), provider)
        for provider in ("tavily", "exa", "linkup", "parallel", "perplexity", "keenable"):
            self.assertFalse(provider_supports_locale(provider), provider)

    def test_builtin_defaults_have_no_provider_locale_keys(self):
        # DEFAULT_CONFIG must not ship provider country/language keys, or the
        # resolver could no longer distinguish "explicitly set in config.json"
        # from a built-in default.
        config = search._deepcopy_default_config()
        for provider, (country_key, language_key) in search_locale.PROVIDER_LOCALE_CONFIG_KEYS.items():
            section = config.get(provider, {})
            if country_key:
                self.assertNotIn(country_key, section, provider)
            if language_key:
                self.assertNotIn(language_key, section, provider)


class LocaleRequestPassThroughTests(unittest.TestCase):
    """Resolved locale must reach the actual provider request bodies."""

    def _isolate(self, stack):
        stack.enter_context(mock.patch.object(search, "provider_in_cooldown", lambda p: (False, 0)))
        stack.enter_context(mock.patch.object(search, "cache_get", lambda **kw: None))
        stack.enter_context(mock.patch.object(search, "cache_put", lambda **kw: None))
        stack.enter_context(mock.patch.object(search, "reset_provider_health", lambda p: None))

    def _run_serper(self, query, config, **kwargs):
        captured = {}
        with contextlib.ExitStack() as stack:
            self._isolate(stack)
            stack.enter_context(mock.patch.dict("os.environ", {"SERPER_API_KEY": "serper-test-key"}))

            def fake_post(url, headers, body, timeout=30):
                captured["url"] = url
                captured["body"] = body
                return {"organic": [{"title": "T", "link": "https://example.test/a", "snippet": "s"}]}

            stack.enter_context(mock.patch("search.make_request", side_effect=fake_post))
            result = search.run_search_request(query=query, provider="serper", config=config, **kwargs)
        return captured, result

    def test_serper_defaults_stay_us_en_without_locale_config(self):
        captured, result = self._run_serper("PostgreSQL 17 release notes", _config())
        self.assertEqual(captured["body"]["gl"], "us")
        self.assertEqual(captured["body"]["hl"], "en")
        self.assertEqual(result["metadata"]["locale"], {
            "country": "us",
            "language": "en",
            "source": {"country": "fallback", "language": "fallback"},
        })

    def test_serper_receives_configured_country_and_inferred_language(self):
        config = _config(locale={"country": "at", "language": "auto"})
        captured, result = self._run_serper("wie funktioniert eine Wärmepumpe im Winter", config)
        self.assertEqual(captured["body"]["gl"], "at")
        self.assertEqual(captured["body"]["hl"], "de")
        self.assertEqual(result["metadata"]["locale"], {
            "country": "at",
            "language": "de",
            "source": {"country": "config", "language": "inferred"},
        })

    def test_serper_location_hint_moves_country_and_language(self):
        config = _config(locale={"country": "at", "language": "auto"})
        captured, result = self._run_serper("mejores restaurantes veganos Madrid", config)
        self.assertEqual(captured["body"]["gl"], "es")
        self.assertEqual(captured["body"]["hl"], "es")
        self.assertEqual(result["metadata"]["locale"]["source"]["country"], "hint")

    def test_serper_cli_flags_beat_config_and_hints(self):
        config = _config(
            locale={"country": "at", "language": "auto"},
            serper={"country": "gb", "language": "en"},
        )
        captured, result = self._run_serper(
            "mejores restaurantes veganos Madrid", config, country="fr", language="fr"
        )
        self.assertEqual(captured["body"]["gl"], "fr")
        self.assertEqual(captured["body"]["hl"], "fr")
        self.assertEqual(result["metadata"]["locale"]["source"], {"country": "cli", "language": "cli"})

    def test_serper_explicit_provider_config_beats_global_defaults(self):
        config = _config(
            locale={"country": "at", "language": "auto"},
            serper={"country": "gb", "language": "en"},
        )
        captured, result = self._run_serper("wie funktioniert eine Wärmepumpe im Winter", config)
        self.assertEqual(captured["body"]["gl"], "gb")
        self.assertEqual(captured["body"]["hl"], "en")
        self.assertEqual(result["metadata"]["locale"]["source"], {"country": "config", "language": "config"})

    def _run_brave(self, query, config, **kwargs):
        captured = {}
        with contextlib.ExitStack() as stack:
            self._isolate(stack)
            stack.enter_context(mock.patch.dict("os.environ", {"BRAVE_API_KEY": "brave-test-key"}))

            def fake_get(url, headers, timeout=30):
                captured["url"] = url
                return {"web": {"results": [{"title": "T", "url": "https://example.test/a", "description": "s"}]}}

            stack.enter_context(mock.patch("search.make_get_request", side_effect=fake_get))
            result = search.run_search_request(query=query, provider="brave", config=config, **kwargs)
        return captured, result

    def test_brave_defaults_stay_us_en_without_locale_config(self):
        captured, _ = self._run_brave("PostgreSQL 17 release notes", _config())
        self.assertIn("country=US", captured["url"])
        self.assertIn("search_lang=en", captured["url"])

    def test_brave_receives_configured_country_and_inferred_language(self):
        config = _config(locale={"country": "at", "language": "auto"})
        captured, result = self._run_brave("wie funktioniert eine Wärmepumpe im Winter", config)
        self.assertIn("country=AT", captured["url"])
        self.assertIn("search_lang=de", captured["url"])
        self.assertEqual(result["metadata"]["locale"], {
            "country": "at",
            "language": "de",
            "source": {"country": "config", "language": "inferred"},
        })

    def test_non_locale_provider_has_no_locale_metadata(self):
        config = _config(locale={"country": "at", "language": "auto"})
        with contextlib.ExitStack() as stack:
            self._isolate(stack)
            stack.enter_context(mock.patch.dict("os.environ", {"TAVILY_API_KEY": "tavily-test-key"}))
            stack.enter_context(mock.patch.object(search, "search_tavily", lambda **kw: {
                "provider": "tavily",
                "query": "q",
                "results": [{"url": "https://example.test/a", "title": "A", "snippet": "s"}],
                "images": [],
                "answer": "",
                "metadata": {},
            }))
            result = search.run_search_request(query="how does HTTPS encryption work", provider="tavily", config=config)
        self.assertNotIn("locale", result.get("metadata", {}))


if __name__ == "__main__":
    unittest.main()
