"""Configurable search locale defaults and query language inference coverage.

Locks down the locale contract: country is config-first (CLI flag > explicit
provider config > query location hint > defaults.locale.country > "us"),
language is query-aware (CLI flag > explicit provider config >
defaults.locale.language > "en"; "auto" detects the query language and sends
none when the detector is not confident), query language never implies a
country, resolved values reach the provider requests, and result metadata
reports where each value came from. All provider calls are mocked; no network
access.
"""
from wsp_core import providers
from wsp_core import config as config_module

import contextlib
import json
import re
import unittest
from pathlib import Path
from unittest import mock

from wsp_core import routing, search_locale
from wsp_core import search
from wsp_core.search_locale import detect_location_country, provider_supports_locale, resolve_locale


BENCHMARKS = Path(__file__).resolve().parent / "fixtures" / "lock"


def _legacy_language_hint(query):
    """Verbatim copy of QueryAnalyzer._detect_language_hint before the merge."""
    q = query.lower()
    if re.search(r'[\u0600-\u06ff]', query):
        return "ar"
    if re.search(r'[\u0400-\u04ff]', query):
        return "ru"
    if re.search(r'[\u3040-\u30ff]', query) or re.search(r'(東京|ニュース|今日|企業|発表)', query):
        return "ja"
    if re.search(r'[\u4e00-\u9fff]', query):
        return "zh"
    if re.search(r'\b(noticias|españa|hoy|regulación|inteligencia artificial)\b', q):
        return "es"
    if re.search(r'\b(actualités|france|aujourd|ouverts?|dimanche|récents?|avis)\b', q):
        return "fr"
    if re.search(r'\b(der|die|das|und|oder|nicht|ist|sind|aktuelle?n?|preis|kaufen|öffnungszeiten|österreich)\b', q):
        return "de"
    return "en"


def _config(locale=None, **provider_overrides):
    """Build a merged runtime config like load_config would produce."""
    config = config_module._deepcopy_default_config()
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


class DetectQueryLanguageTests(unittest.TestCase):
    """One detector: ``hint`` feeds routing, ``inferred`` feeds locale."""

    def test_infer_query_language_is_the_inferred_view(self):
        for query in (
            "wie funktioniert eine Wärmepumpe im Winter",
            "PostgreSQL 17 release notes",
            "最新ニュース 日本 経済",
            "",
        ):
            self.assertEqual(
                routing.infer_query_language(query),
                routing.detect_query_language(query).inferred,
            )

    def test_hint_is_trigger_happy_while_inference_stays_conservative(self):
        # One trigger word is enough for the routing hint ...
        detected = routing.detect_query_language("capital of France")
        self.assertEqual((detected.hint, detected.inferred), ("fr", None))
        # ... and a query with two German signals infers de although the hint is en.
        detected = routing.detect_query_language("Wiener Linien Störung heute")
        self.assertEqual((detected.hint, detected.inferred), ("en", "de"))

    def test_hint_defaults_to_en(self):
        self.assertEqual(routing.detect_query_language("PostgreSQL 17 release notes").hint, "en")
        self.assertEqual(routing.detect_query_language("").hint, "en")
        self.assertEqual(routing.detect_query_language(None).hint, "en")

    def test_kana_infers_japanese(self):
        detected = routing.detect_query_language("最新ニュース 日本 経済")
        self.assertEqual((detected.hint, detected.inferred), ("ja", "ja"))

    def test_russian_and_arabic_script_infer_their_language(self):
        self.assertEqual(routing.detect_query_language("что такое HTTPS").inferred, "ru")
        self.assertEqual(routing.detect_query_language("أخبار اليوم").inferred, "ar")

    def test_han_only_and_japanese_word_list_stay_hint_only(self):
        # Han-only text is zh or ja; 東京 is also traditional Chinese.
        detected = routing.detect_query_language("最新 新闻")
        self.assertEqual((detected.hint, detected.inferred), ("zh", None))
        detected = routing.detect_query_language("東京 天気 明日")
        self.assertEqual((detected.hint, detected.inferred), ("ja", None))

    def test_cyrillic_and_arabic_script_of_other_languages_stay_hint_only(self):
        # Ukrainian letters and Persian letters share the script but not the language.
        detected = routing.detect_query_language("новини України їжак")
        self.assertEqual((detected.hint, detected.inferred), ("ru", None))
        detected = routing.detect_query_language("اخبار امروز گربه")
        self.assertEqual((detected.hint, detected.inferred), ("ar", None))

    def test_conflicting_script_and_stopwords_infer_nothing(self):
        # Kana says ja, the English stopwords say en: ambiguous.
        detected = routing.detect_query_language("best ramen near me ラーメン")
        self.assertEqual((detected.hint, detected.inferred), ("ja", None))

    def test_unconfident_script_does_not_hide_latin_evidence(self):
        detected = routing.detect_query_language("the best 新闻 sites near me")
        self.assertEqual((detected.hint, detected.inferred), ("zh", "en"))

    def test_routing_and_locale_call_the_same_detector(self):
        calls = []
        real = routing.detect_query_language

        def spy(query):
            calls.append(query)
            return real(query)

        config = _config(locale={"language": "auto"})
        with mock.patch.object(routing, "detect_query_language", spy), \
                mock.patch.object(search_locale, "detect_query_language", spy):
            routing.route_query("wie funktioniert eine Wärmepumpe", config)
            resolve_locale("serper", config, "wie funktioniert eine Wärmepumpe")
        self.assertEqual(calls, ["wie funktioniert eine Wärmepumpe"] * 2)

    def test_routing_hint_matches_the_pre_merge_routing_detector(self):
        # The language hint must not move: compare against the previous
        # QueryAnalyzer._detect_language_hint on the evaluation queries and
        # on script / trigger-word edge cases.
        queries = [
            json.loads(line)["query"]
            for name in ("queries.jsonl", "tuning_queries.jsonl")
            for line in (BENCHMARKS / name).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        queries += [
            "", "   ", "capital of France", "die hard", "avis clients", "Hoy HOY",
            "что такое HTTPS", "новини України", "أخبار اليوم", "اخبار امروز",
            "最新ニュース", "東京 天気", "最新 新闻", "서울 뉴스", "ラーメン best near me",
            "inteligencia artificial hoy", "Wiener Linien Störung heute",
        ]
        config = _config()
        for query in queries:
            self.assertEqual(
                routing.detect_query_language(query).hint,
                _legacy_language_hint(query),
                query,
            )
            self.assertEqual(
                routing.route_query(query, config)["analysis_summary"]["language_hint"],
                _legacy_language_hint(query),
                query,
            )


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

    def test_auto_without_confident_detection_sends_no_language(self):
        config = _config(locale={"country": "at", "language": "auto"})
        country, language, meta = resolve_locale("serper", config, "DAC R2R NOS")
        self.assertEqual((country, language), ("at", None))
        self.assertEqual(meta["source"]["language"], "provider_default")

    def test_cli_auto_detects_instead_of_sending_the_word_auto(self):
        config = _config()
        _, language, meta = resolve_locale(
            "serper", config, "wie funktioniert eine Wärmepumpe im Winter", cli_language="auto"
        )
        self.assertEqual(language, "de")
        self.assertEqual(meta["source"]["language"], "inferred")
        _, language, meta = resolve_locale("serper", config, "PostgreSQL 17 release notes", cli_language=" AUTO ")
        self.assertIsNone(language)
        self.assertEqual(meta["source"]["language"], "provider_default")

    def test_cli_auto_beats_a_concrete_default_language(self):
        config = _config(locale={"language": "fr"})
        _, language, meta = resolve_locale(
            "serper", config, "wie funktioniert eine Wärmepumpe im Winter", cli_language="auto"
        )
        self.assertEqual((language, meta["source"]["language"]), ("de", "inferred"))

    def test_explicit_provider_config_still_beats_auto(self):
        config = _config(locale={"language": "auto"}, serper={"language": "en"})
        _, language, meta = resolve_locale(
            "serper", config, "wie funktioniert eine Wärmepumpe im Winter", cli_language="auto"
        )
        self.assertEqual((language, meta["source"]["language"]), ("en", "config"))

    def test_apply_auto_language_moves_auto_into_a_config_copy(self):
        config = _config(locale={"country": "at", "language": "fr"})
        language, patched = search_locale.apply_auto_language(" Auto ", config)
        self.assertIsNone(language)
        self.assertEqual(patched["defaults"]["locale"], {"country": "at", "language": "auto"})
        self.assertEqual(config["defaults"]["locale"], {"country": "at", "language": "fr"})

    def test_apply_auto_language_leaves_other_values_alone(self):
        config = _config()
        for value in (None, "", "de"):
            language, same = search_locale.apply_auto_language(value, config)
            self.assertEqual(language, value)
            self.assertIs(same, config)

    def test_apply_auto_language_tolerates_missing_locale_section(self):
        config = _config()
        config["defaults"].pop("locale")
        _, patched = search_locale.apply_auto_language("auto", config)
        self.assertEqual(patched["defaults"]["locale"], {"language": "auto"})
        config.pop("defaults")
        _, patched = search_locale.apply_auto_language("auto", config)
        self.assertEqual(patched["defaults"]["locale"], {"language": "auto"})

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
        for provider in ("tavily", "exa", "linkup", "parallel", "keenable"):
            self.assertFalse(provider_supports_locale(provider), provider)

    def test_builtin_defaults_have_no_provider_locale_keys(self):
        # DEFAULT_CONFIG must not ship provider country/language keys, or the
        # resolver could no longer distinguish "explicitly set in config.json"
        # from a built-in default.
        config = config_module._deepcopy_default_config()
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

            stack.enter_context(mock.patch("wsp_core.providers.make_request", side_effect=fake_post))
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

            def fake_get(url, headers, timeout=30, **_kwargs):
                captured["url"] = url
                return {"web": {"results": [{"title": "T", "url": "https://example.test/a", "description": "s"}]}}

            stack.enter_context(mock.patch("wsp_core.providers.make_get_request", side_effect=fake_get))
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

    def test_serper_tool_language_auto_detects_the_query_language(self):
        captured, result = self._run_serper(
            "wie funktioniert eine Wärmepumpe im Winter", _config(), language="auto"
        )
        self.assertEqual(captured["body"]["hl"], "de")
        self.assertEqual(captured["body"]["gl"], "us")
        self.assertEqual(result["metadata"]["locale"]["source"], {"country": "fallback", "language": "inferred"})

    def test_serper_tool_language_auto_omits_hl_when_not_confident(self):
        captured, result = self._run_serper("PostgreSQL 17 release notes", _config(), language="auto")
        self.assertNotIn("hl", captured["body"])
        self.assertEqual(captured["body"]["gl"], "us")
        self.assertEqual(result["metadata"]["locale"], {
            "country": "us",
            "language": None,
            "source": {"country": "fallback", "language": "provider_default"},
        })

    def test_serper_tool_language_auto_beats_a_concrete_default(self):
        config = _config(locale={"language": "fr"})
        captured, _ = self._run_serper("wie funktioniert eine Wärmepumpe im Winter", config, language="auto")
        self.assertEqual(captured["body"]["hl"], "de")

    def test_serper_without_language_option_keeps_sending_en_for_unclear_queries(self):
        captured, _ = self._run_serper("PostgreSQL 17 release notes", _config())
        self.assertEqual(captured["body"]["hl"], "en")

    def test_brave_tool_language_auto_omits_search_lang_when_not_confident(self):
        captured, result = self._run_brave("PostgreSQL 17 release notes", _config(), language="auto")
        self.assertIn("country=US", captured["url"])
        self.assertNotIn("search_lang", captured["url"])
        self.assertIsNone(result["metadata"]["locale"]["language"])

    def test_brave_tool_language_auto_sends_the_detected_language(self):
        captured, _ = self._run_brave("wie funktioniert eine Wärmepumpe im Winter", _config(), language="auto")
        self.assertIn("search_lang=de", captured["url"])

    def test_cache_context_distinguishes_auto_without_language_from_en(self):
        auto = search.default_search_args(_config())
        auto.query = "PostgreSQL 17 release notes"
        auto.language = "auto"
        plain = search.default_search_args(_config())
        plain.query = auto.query
        config = _config(locale={"language": "auto"})
        auto_locale = search._legacy_search_cache_context(auto, "serper", config)["locale"]
        plain_locale = search._legacy_search_cache_context(plain, "serper", _config())["locale"]
        self.assertEqual((auto_locale, plain_locale), ("us:", "us:en"))

    def test_non_locale_provider_has_no_locale_metadata(self):
        config = _config(locale={"country": "at", "language": "auto"})
        with contextlib.ExitStack() as stack:
            self._isolate(stack)
            stack.enter_context(mock.patch.dict("os.environ", {"TAVILY_API_KEY": "tavily-test-key"}))
            stack.enter_context(mock.patch.object(providers, "search_tavily", lambda **kw: {
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
