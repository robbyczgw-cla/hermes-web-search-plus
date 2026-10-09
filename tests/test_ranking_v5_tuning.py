"""Locale inference, vendor boost, rerank window and overfetch trim."""
import copy

from wsp_core import quality, routing
from wsp_core.config import DEFAULT_CONFIG
from wsp_core.search_locale import resolve_locale


def _locale(query):
    return resolve_locale("brave", copy.deepcopy(DEFAULT_CONFIG), query)[2]


def test_one_strong_word_identifies_a_language_without_english_signal():
    assert routing.detect_query_language("Bundestag Abstimmung heute Ergebnis").inferred == "de"
    assert routing.detect_query_language("vulnérabilité critique Ivanti").inferred == "fr"


def test_strong_words_never_override_english_stopwords():
    assert routing.detect_query_language("Avis car rental").inferred is None
    assert routing.detect_query_language("what is heute in the news").inferred in (None, "en")


def test_detected_language_picks_language_and_home_country_when_unconfigured():
    meta = _locale("annuler une requête fetch avec AbortController")
    assert (meta["country"], meta["language"]) == ("fr", "fr")
    assert meta["source"] == {"country": "hint", "language": "inferred"}


def test_a_named_place_keeps_its_country_over_the_language_home():
    meta = _locale("Hurrikan aktuelle Lage USA")
    assert meta["country"] == "us"
    assert meta["language"] == "de"


def test_undetected_queries_keep_the_english_fallback():
    meta = _locale("PostgreSQL 17 release notes")
    assert (meta["country"], meta["language"]) == ("us", "en")


def test_vendor_domain_matches_the_registrable_label_only():
    terms = quality._query_terms("TypeScript satisfies operator")
    assert quality._is_named_vendor_domain("https://www.typescriptlang.org/docs", terms)
    assert not quality._is_named_vendor_domain("https://typescript.website/x", terms)
    terms = quality._query_terms("fastapi dependency injection")
    assert not quality._is_named_vendor_domain("https://injection.readthedocs.io/", terms)
    assert not quality._is_named_vendor_domain("https://github.com/fastapi", terms)


def test_named_vendor_outranks_generic_docs_boost():
    results = [
        {"url": "https://github.com/someone/react-hooks"},
        {"url": "https://react.dev/reference/react/useEffect"},
    ]
    ranked, _ = quality.rerank_results_for_intent("React useEffect cleanup", "docs", results)
    assert ranked[0]["url"].startswith("https://react.dev")


def test_rerank_window_keeps_spare_results_behind_the_top_n():
    results = [{"url": f"https://blog{i}.example/x"} for i in range(5)]
    results.append({"url": "https://readthedocs.io/en/latest/serialization.html"})
    # A generic boosted host among the spares must not leapfrog the top N.
    ranked, _ = quality.rerank_results_for_intent("object serialization guide", "docs", results, window=5)
    assert ranked[-1]["url"].startswith("https://readthedocs.io")


def test_diversity_cap_spares_the_vendor_the_query_names():
    results = [{"url": f"https://react.dev/p{i}"} for i in range(4)] + [{"url": "https://blog.example/x"}]
    ranked, demoted = quality.rerank_domain_diversity(results, max_per_domain=2, query="React useEffect")
    assert demoted == 0
    ranked, demoted = quality.rerank_domain_diversity(results, max_per_domain=2, query="hooks tutorial")
    assert demoted == 2 and ranked[2]["url"] == "https://blog.example/x"


def test_stackexchange_is_a_forum_even_under_a_security_label():
    results = [
        {"url": "https://security.stackexchange.com/q/1"},
        {"url": "https://docs.python.org/3/library/pickle.html"},
    ]
    ranked, _ = quality.rerank_results_for_intent("python pickle deserialization risk", "security", results)
    assert ranked[0]["url"].startswith("https://docs.python.org")


def test_social_profiles_are_replaced_by_spares_in_security():
    results = [{"url": "https://www.facebook.com/x"}, {"url": "https://heise.de/a"}]
    results += [{"url": f"https://site{i}.example/x"} for i in range(3)]
    results.append({"url": "https://www.bsi.bund.de/advisory"})
    ranked, _ = quality.rerank_results_for_intent("Exchange Sicherheitslücke", "security", results, window=5)
    top = [item["url"] for item in ranked[:5]]
    assert "https://www.facebook.com/x" not in top
    assert "https://www.bsi.bund.de/advisory" in top


def test_social_profiles_stay_for_local_queries():
    results = [{"url": "https://www.facebook.com/baeckerei"}] + [{"url": f"https://s{i}.example"} for i in range(6)]
    ranked, _ = quality.rerank_results_for_intent("Bäckerei Graz", "local", results, window=5)
    assert ranked[0]["url"] == "https://www.facebook.com/baeckerei"


def test_python_docs_lookalikes_are_spam():
    kept, removed = quality.filter_spam_results([
        {"url": "https://docs.python.domainunion.de/3/library/dataclasses.html"},
        {"url": "https://docs.python.org/3/library/dataclasses.html"},
    ])
    assert [item["url"] for item in kept] == ["https://docs.python.org/3/library/dataclasses.html"]


def test_one_spare_from_the_named_source_takes_the_last_slot_in_news():
    results = [{"url": f"https://paper{i}.example/x"} for i in range(5)]
    results.append({"url": "https://www.bundestag.de/abstimmung"})
    ranked, _ = quality.rerank_results_for_intent("Bundestag Abstimmung heute Ergebnis", "news", results, window=5)
    assert "https://www.bundestag.de/abstimmung" in [item["url"] for item in ranked[:5]]
    assert "https://paper4.example/x" not in [item["url"] for item in ranked[:5]]


def test_no_vendor_promotion_when_the_top_n_already_has_it():
    results = [{"url": "https://react.dev/a"}] + [{"url": f"https://b{i}.example"} for i in range(4)]
    results.append({"url": "https://react.dev/b"})
    ranked, _ = quality.rerank_results_for_intent("React useEffect", "docs", results, window=5)
    assert [item["url"] for item in ranked[5:]] == ["https://react.dev/b"]
