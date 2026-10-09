"""Routing v3: the measured intent table, its fall-through, migration and reporting.

The first provider for a query comes from its intent (wsp_core/intents.py) and a
small measured table: academic and docs start with Exa, shopping with Serper,
everything else with Brave. A provider that is not configured (or is disabled or
not auto-allowed) is skipped, and the user's provider_priority only orders the
fallback chain after the first provider. No test here touches the network: keys
are dummy strings in the config and provider calls are mocked.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest

from plugin_loader import load_plugin
from wsp_core import config as config_module
from wsp_core import providers, provider_stats, quality, routing, search
from wsp_core.compat_v3 import legacy_request_to_v3
from wsp_core.config import _deepcopy_default_config, load_config
from wsp_core.contract_v3 import Capability
from wsp_core.intents import INTENTS, IntentDecision, classify_intent
from wsp_core.operator_privacy_v3 import assert_operator_payload_safe
from wsp_core.provider_registry import (
    DEFAULT_PROVIDER_PRIORITY,
    PRE_5_DEFAULT_PROVIDER_PRIORITY,
)

plugin = load_plugin("wsp_plugin_test_routing_v3")
ROOT = Path(__file__).resolve().parents[1]

CORE = ("brave", "serper", "exa", "tavily")
EVERY_KEYED = CORE + ("you", "linkup", "firecrawl", "parallel")

# One clear query per intent (the intent is asserted again in the tests).
QUERY = {
    "academic": "systematic review of sleep interventions in adolescents",
    "community": "site:reddit.com r/hometheater Denon X4800H user impressions HDMI issues",
    "docs": "python asyncio gather documentation",
    "general": "apple pie recipe",
    "local": "restaurants near me open now",
    "news": "breaking news tonight",
    "security": "CVE-2023-4863 heap overflow details",
    "shopping": "best wireless earbuds under $100",
}
FIRST_WITH_CORE = {
    "academic": "exa",
    "community": "brave",
    "docs": "exa",
    "general": "brave",
    "local": "brave",
    "news": "brave",
    "security": "serper",
    "shopping": "serper",
}


def _config(providers=CORE, **auto):
    """A runtime config with a dummy key for each of ``providers``."""
    config = _deepcopy_default_config()
    for provider in providers:
        config[provider] = {**config.get(provider, {}), "api_key": f"{provider}-test-key-123456"}
    config["auto_routing"].update(auto)
    return config


def _first(query, config):
    return search.auto_route_provider(query, config)["provider"]


def _plan(query, config):
    request = legacy_request_to_v3(
        Capability.SEARCH, {"query": query, "provider": "auto", "count": 3}, request_id="routing-v3"
    )
    return search._plan_search_v3(request, config)


# --- the table ---------------------------------------------------------------


def test_the_table_is_what_the_benchmarks_measured():
    assert routing.INTENT_FIRST_PROVIDER == {
        "academic": "exa",
        "docs": "exa",
        "security": "serper",
        "shopping": "serper",
    }
    assert routing.MEASURED_PROVIDER_ORDER == ("brave", "serper", "exa", "tavily")
    assert routing.ROUTING_POLICY == quality.ROUTING_POLICY == search.ROUTING_POLICY == "routing-v3"


@pytest.mark.parametrize("intent", sorted(QUERY))
def test_first_provider_follows_the_intent_table_with_all_four_configured(intent):
    query = QUERY[intent]

    decision = search.auto_route_provider(query, _config())

    assert classify_intent(query).intent == intent
    assert decision["analysis_summary"]["routing_class"] == intent
    assert decision["provider"] == FIRST_WITH_CORE[intent]
    assert decision["routing_policy"] == "routing-v3"


def test_the_table_covers_every_intent():
    assert sorted(QUERY) == sorted(INTENTS)
    assert sorted(FIRST_WITH_CORE) == sorted(INTENTS)


@pytest.mark.parametrize(
    "intent, configured, expected",
    [
        # Exa is missing: docs and academic fall to the measured order.
        ("docs", ("brave", "serper", "tavily"), "brave"),
        ("academic", ("brave", "serper", "tavily"), "brave"),
        # Serper is missing: shopping falls to Brave.
        ("shopping", ("brave", "exa", "tavily"), "brave"),
        # The measured order itself is walked in order.
        ("docs", ("serper", "tavily"), "serper"),
        ("docs", ("tavily",), "tavily"),
        ("shopping", ("exa", "tavily"), "exa"),
        ("general", ("serper", "exa", "tavily"), "serper"),
        ("general", ("exa", "tavily"), "exa"),
        ("general", ("tavily",), "tavily"),
    ],
)
def test_an_intent_rule_falls_through_the_measured_order_when_its_provider_is_missing(
    intent, configured, expected
):
    assert _first(QUERY[intent], _config(configured)) == expected


def test_docs_without_an_exa_key_goes_to_brave_even_when_exa_leads_the_user_priority():
    config = _config(("brave", "serper", "tavily"), provider_priority=["exa", "tavily", "serper", "brave"])

    decision = search.auto_route_provider(QUERY["docs"], config)

    assert decision["provider"] == "brave"
    assert "exa" not in decision["candidate_order"]


def test_disabled_providers_are_never_first():
    config = _config(disabled_providers=["exa"])
    assert _first(QUERY["docs"], config) == "brave"
    assert "exa" not in search.auto_route_provider(QUERY["docs"], config)["candidate_order"]

    config = _config(disabled_providers=["brave"])
    assert _first(QUERY["general"], config) == "serper"

    config = _config(disabled_providers=["serper"])
    assert _first(QUERY["shopping"], config) == "brave"


def test_auto_allow_false_providers_are_never_first_and_are_reported():
    config = _config(auto_allow={"exa": False})

    decision = search.auto_route_provider(QUERY["docs"], config)

    assert decision["provider"] == "brave"
    assert "exa" not in decision["candidate_order"]
    assert decision["auto_allow_excluded"] == ["exa"]

    config = _config(auto_allow={"brave": False})
    assert _first(QUERY["general"], config) == "serper"


def test_a_guarded_provider_first_in_the_user_priority_does_not_lead():
    config = _config(("querit", "serper"), provider_priority=["querit", "serper"])

    decision = search.auto_route_provider(QUERY["general"], config)

    assert decision["provider"] == "serper"
    assert decision["auto_allow_excluded"] == ["querit"]


@pytest.mark.parametrize("intent", ["docs", "shopping", "general"])
def test_without_any_measured_provider_the_first_provider_in_the_user_priority_wins(intent):
    config = _config(("you", "linkup", "firecrawl"), provider_priority=["firecrawl", "you", "linkup"])
    assert _first(QUERY[intent], config) == "firecrawl"

    config = _config(("you", "linkup", "firecrawl"), provider_priority=["linkup", "firecrawl", "you"])
    assert _first(QUERY[intent], config) == "linkup"


def test_a_measured_provider_beats_the_user_priority_for_the_first_slot():
    config = _config(("you", "linkup", "tavily"), provider_priority=["you", "linkup", "tavily"])

    assert _first(QUERY["general"], config) == "tavily"


def test_no_configured_provider_falls_back_to_fallback_provider_with_zero_confidence():
    config = _config(())
    config["auto_routing"]["fallback_provider"] = "serper"

    decision = search.auto_route_provider(QUERY["docs"], config)

    assert decision["provider"] == "serper"
    assert decision["reason"] == "no_available_providers"
    assert decision["confidence"] == 0.0
    assert decision["confidence_level"] == "low"
    assert decision["candidate_order"] == []


# --- the user's provider_priority orders the fallback chain only ----------------


@pytest.mark.parametrize(
    "priority",
    [
        ["tavily", "exa", "serper", "brave"],
        ["you", "linkup", "firecrawl", "parallel", "tavily", "exa", "serper", "brave"],
        ["serper", "brave", "exa", "tavily"],
    ],
)
@pytest.mark.parametrize("intent", ["docs", "shopping", "general"])
def test_user_priority_changes_the_fallback_chain_but_not_the_first_provider(intent, priority):
    config = _config(EVERY_KEYED, provider_priority=list(priority))

    plan = _plan(QUERY[intent], config)

    first = FIRST_WITH_CORE[intent]
    assert plan.selected_provider == first
    assert plan.candidate_order[0] == first
    assert list(plan.candidate_order[1:]) == [p for p in priority if p != first]


def test_the_fallback_chain_after_the_first_provider_is_the_default_priority():
    config = _config(EVERY_KEYED)

    plan = _plan(QUERY["docs"], config)

    expected = ["exa"] + [p for p in DEFAULT_PROVIDER_PRIORITY if p in EVERY_KEYED and p != "exa"]
    assert list(plan.candidate_order) == expected


def test_the_chain_skips_providers_that_are_not_configured_or_disabled():
    config = _config(
        ("brave", "exa", "you", "linkup"),
        provider_priority=["linkup", "you", "exa", "brave"],
        disabled_providers=["you"],
    )

    plan = _plan(QUERY["general"], config)

    assert list(plan.candidate_order) == ["brave", "linkup", "exa"]


def test_the_planner_hands_the_intent_to_the_cache_ttl():
    plan = _plan(QUERY["security"], _config())

    assert plan.routing_metadata["analysis_summary"]["routing_class"] == "security"


# --- recorded provider performance no longer steers routing ---------------------


def test_recorded_provider_performance_does_not_change_the_route(monkeypatch):
    config = _config()
    before = search.auto_route_provider(QUERY["general"], config)
    for _ in range(20):
        provider_stats.record_provider_outcome("brave", 30.0, 0, True)
        provider_stats.record_provider_outcome("serper", 0.1, 10, False)

    after = search.auto_route_provider(QUERY["general"], config)

    assert before["provider"] == after["provider"] == "brave"
    assert before == after
    assert not hasattr(provider_stats, "performance_adjustments")
    assert not hasattr(provider_stats, "performance_adjustment")


@pytest.mark.parametrize("value", [True, False])
def test_the_adaptive_routing_config_key_is_still_accepted_and_has_no_effect(
    value, tmp_path, monkeypatch
):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"auto_routing": {"adaptive_routing": value, "provider_priority": ["serper", "brave"]}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("WEB_SEARCH_PLUS_CONFIG", str(config_path))

    loaded = load_config()
    merged = plugin._merge_behavior_config(json.loads(config_path.read_text(encoding="utf-8")))

    assert loaded["auto_routing"]["provider_priority"][:2] == ["serper", "brave"]
    assert merged["auto_routing"]["provider_priority"][:2] == ["serper", "brave"]
    keyed = {**loaded, **{p: {"api_key": f"{p}-test-key-123456"} for p in CORE}}
    assert search.auto_route_provider(QUERY["general"], keyed)["provider"] == "brave"
    assert search.auto_route_provider(QUERY["docs"], keyed)["provider"] == "exa"


# --- config migration ---------------------------------------------------------------


def _write_config(tmp_path, monkeypatch, priority):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"auto_routing": {"provider_priority": list(priority)}}), encoding="utf-8")
    monkeypatch.setenv("WEB_SEARCH_PLUS_CONFIG", str(path))
    return path


def test_the_4x_default_priority_loads_as_the_new_default_order(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, PRE_5_DEFAULT_PROVIDER_PRIORITY)

    loaded = load_config()["auto_routing"]["provider_priority"]

    assert loaded[:12] == list(DEFAULT_PROVIDER_PRIORITY)
    assert loaded[:4] == ["brave", "serper", "exa", "tavily"]


def test_the_4x_default_priority_with_extra_providers_keeps_the_extras_after_the_new_default(
    tmp_path, monkeypatch
):
    _write_config(tmp_path, monkeypatch, [*PRE_5_DEFAULT_PROVIDER_PRIORITY, "donsetch", "octen"])

    loaded = load_config()["auto_routing"]["provider_priority"]

    assert loaded[:12] == list(DEFAULT_PROVIDER_PRIORITY)
    assert loaded[12:14] == ["donsetch", "octen"]
    assert len(loaded) == len(set(loaded))


def test_a_custom_priority_is_left_as_it_is(tmp_path, monkeypatch):
    custom = ["serper", "you", "brave"]
    _write_config(tmp_path, monkeypatch, custom)

    loaded = load_config()["auto_routing"]["provider_priority"]

    assert loaded[:3] == custom
    # Only newly introduced providers are appended, in default order.
    assert loaded[3:] == [p for p in DEFAULT_PROVIDER_PRIORITY if p not in custom]


def test_the_4x_default_with_two_providers_swapped_counts_as_custom(tmp_path, monkeypatch):
    swapped = list(PRE_5_DEFAULT_PROVIDER_PRIORITY)
    swapped[0], swapped[1] = swapped[1], swapped[0]
    _write_config(tmp_path, monkeypatch, swapped)

    assert load_config()["auto_routing"]["provider_priority"][:12] == swapped


def test_the_plugin_merge_turns_the_4x_default_into_the_new_default_order():
    user = {"auto_routing": {"provider_priority": list(PRE_5_DEFAULT_PROVIDER_PRIORITY)}}

    merged = plugin._merge_behavior_config(user)["auto_routing"]["provider_priority"]

    assert merged[:12] == list(DEFAULT_PROVIDER_PRIORITY)


def test_the_plugin_merge_accepts_the_4x_default_as_a_csv_string_with_extras():
    csv = ",".join([*PRE_5_DEFAULT_PROVIDER_PRIORITY, "tinyfish"])

    merged = plugin._merge_behavior_config({"auto_routing": {"provider_priority": csv}})
    priority = merged["auto_routing"]["provider_priority"]

    assert priority[:12] == list(DEFAULT_PROVIDER_PRIORITY)
    assert priority[12] == "tinyfish"


def test_the_plugin_merge_keeps_a_custom_priority():
    user = {"auto_routing": {"provider_priority": ["linkup", "you", "serper"]}}

    merged = plugin._merge_behavior_config(user)["auto_routing"]["provider_priority"]

    assert merged[:3] == ["linkup", "you", "serper"]


def test_the_plugin_merge_keeps_the_4x_default_as_a_custom_order(tmp_path, monkeypatch):
    # Same rule as the runtime: a list the user chose with order "custom" is theirs,
    # even when it happens to equal what 4.x wrote by default.
    legacy = list(PRE_5_DEFAULT_PROVIDER_PRIORITY)
    user = {"auto_routing": {"order": "custom", "provider_priority": legacy}}

    merged = plugin._merge_behavior_config(user)["auto_routing"]["provider_priority"]
    path = tmp_path / "config.json"
    path.write_text(json.dumps(user), encoding="utf-8")
    monkeypatch.setenv("WEB_SEARCH_PLUS_CONFIG", str(path))
    runtime = load_config()["auto_routing"]["provider_priority"]

    assert merged[:12] == legacy
    assert runtime[:12] == legacy


def test_the_migrated_default_drives_the_first_provider_like_a_fresh_install(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, PRE_5_DEFAULT_PROVIDER_PRIORITY)
    migrated = load_config()
    for provider in EVERY_KEYED:
        migrated[provider] = {**migrated.get(provider, {}), "api_key": f"{provider}-test-key-123456"}

    assert _first(QUERY["general"], migrated) == "brave"
    assert [p for p in _plan(QUERY["general"], migrated).candidate_order][:4] == list(CORE)


# --- the five known misroutes of 4.3.5 ----------------------------------------------


@pytest.mark.parametrize("providers", [CORE, EVERY_KEYED], ids=["core4", "every-key"])
@pytest.mark.parametrize(
    "query, expected_intent, expected_provider",
    [
        ("what are the CVEs in openssl 3.2", "security", "serper"),
        ("nvidia driver install linux", "general", "brave"),
        ("apple pie recipe", "general", "brave"),
        ("hotels in salzburg", "general", "brave"),
        ("vegan cake recipe", "general", "brave"),
    ],
)
def test_the_4x_misroutes_are_fixed(query, expected_intent, expected_provider, providers):
    decision = search.auto_route_provider(query, _config(providers))

    assert decision["provider"] == expected_provider
    assert decision["analysis_summary"]["routing_class"] == expected_intent
    assert decision["analysis_summary"]["routing_class"] in INTENTS


def test_an_nvidia_driver_query_is_neither_finance_nor_linkup():
    decision = search.auto_route_provider("nvidia driver install linux", _config(EVERY_KEYED))

    assert decision["provider"] != "linkup"
    assert "finance" not in decision["analysis_summary"]["routing_class"]
    assert all("finance" not in signal for signal in decision["analysis_summary"]["intent_signals"])


def test_a_hotel_query_with_a_place_name_is_local_and_goes_to_brave():
    decision = search.auto_route_provider("hotels in Salzburg", _config())

    assert decision["analysis_summary"]["routing_class"] == "local"
    assert decision["provider"] == "brave"


# --- no overfit terms, no hard-coded years ---------------------------------------------


@pytest.mark.parametrize("module", ["routing.py", "intents.py"])
def test_the_router_sources_name_no_tuned_query_terms_and_no_years(module):
    text = (ROOT / "wsp_core" / module).read_text(encoding="utf-8")

    for word in (r"graz", r"sturm", r"lask"):
        assert not re.search(rf"\b{word}\b", text, re.IGNORECASE), word
    assert not re.search(r"hifi[\s_-]?team", text, re.IGNORECASE)
    assert not re.search(r"\b202[0-9]\b", text)


# --- recency ----------------------------------------------------------------------------


def test_detect_recency_counts_the_given_year_and_the_next_one():
    assert routing.detect_recency("best laptop 2026", year=2026) == (False, 2.0)
    assert routing.detect_recency("best laptop 2027", year=2026) == (False, 2.0)
    assert routing.detect_recency("best laptop 2026 latest", year=2026)[0] is True


def test_detect_recency_ignores_other_years():
    assert routing.detect_recency("2024 elections", year=2026) == (False, 0.0)
    assert routing.detect_recency("best laptop 2025", year=2026) == (False, 0.0)
    assert routing.detect_recency("best laptop 2028", year=2026) == (False, 0.0)
    assert routing.detect_recency("2024 elections", year=2024)[1] == 2.0


def test_detect_recency_flags_recency_words():
    flagged, score = routing.detect_recency("latest news today")

    assert flagged is True
    assert score >= 5.0
    assert routing.detect_recency("how does a heat pump work") == (False, 0.0)
    assert routing.detect_recency("") == (False, 0.0)


def test_detect_recency_defaults_to_the_current_year():
    year = time.gmtime().tm_year

    assert routing.detect_recency(f"best laptop {year}")[1] == 2.0
    assert routing.detect_recency(f"best laptop {year + 1}")[1] == 2.0
    assert routing.detect_recency(f"best laptop {year - 2}")[1] == 0.0


def test_the_routing_summary_reports_recency():
    config = _config()

    assert search.auto_route_provider("latest news today", config)["analysis_summary"]["recency_focused"] is True
    assert search.auto_route_provider(QUERY["general"], config)["analysis_summary"]["recency_focused"] is False


# --- the decision and explain shapes -------------------------------------------------------


def test_the_routing_decision_shape():
    decision = search.auto_route_provider(QUERY["docs"], _config())

    assert set(decision) == {
        "provider", "confidence", "confidence_level", "reason", "routing_policy", "exa_depth",
        "scores", "top_signals", "candidate_order", "auto_allow_excluded", "analysis_summary",
    }
    assert decision["scores"] == {}
    assert decision["exa_depth"] == "normal"
    assert decision["reason"] == "intent_docs"
    assert decision["candidate_order"][0] == "exa"
    assert decision["top_signals"]
    assert all(set(item) == {"matched", "weight"} for item in decision["top_signals"])
    assert set(decision["analysis_summary"]) == {
        "query_length", "has_url", "recency_focused", "language_hint", "routing_class", "intent_signals",
    }
    assert decision["analysis_summary"]["query_length"] == len(QUERY["docs"].split())
    assert decision["top_signals"] == [
        {"matched": signal, "weight": 1.0}
        for signal in decision["analysis_summary"]["intent_signals"][:5]
    ]
    assert decision["confidence"] == round(classify_intent(QUERY["docs"]).confidence, 3)


def test_a_query_without_signals_reports_no_signals_matched():
    decision = search.auto_route_provider(QUERY["general"], _config())

    assert decision["reason"] == "no_signals_matched"
    assert decision["top_signals"] == []
    assert decision["analysis_summary"]["routing_class"] == "general"


def test_has_url_is_reported():
    decision = search.auto_route_provider("summarize https://example.com/page please", _config())

    assert decision["analysis_summary"]["has_url"] is True


@pytest.mark.parametrize(
    "confidence, level",
    [(0.95, "high"), (0.7, "high"), (0.699, "medium"), (0.4, "medium"), (0.399, "low"), (0.0, "low")],
)
def test_confidence_level_thresholds(confidence, level, monkeypatch):
    monkeypatch.setattr(
        routing, "classify_intent", lambda query: IntentDecision("docs", confidence, ("docs:docs_word",))
    )

    decision = routing.route_query("anything", _config())

    assert decision["confidence_level"] == level
    assert decision["confidence"] == round(confidence, 3)


def test_auto_routing_disabled_uses_the_default_provider():
    config = _config(enabled=False)
    config["default_provider"] = "tavily"

    decision = search.auto_route_provider(QUERY["docs"], config)

    assert decision["provider"] == "tavily"
    assert decision["reason"] == "auto_routing_disabled_default_provider"
    assert decision["auto_routed"] is False


def test_explain_routing_output_shape():
    config = _config(EVERY_KEYED, disabled_providers=["parallel"])
    config["auto_routing"]["auto_allow"] = {"linkup": False}

    explanation = search.explain_routing(QUERY["docs"], config)

    assert set(explanation) == {
        "query", "routing_decision", "intent", "first_provider_rules", "measured_order",
        "query_analysis", "available_providers",
    }
    assert explanation["query"] == QUERY["docs"]
    assert set(explanation["routing_decision"]) == {
        "provider", "confidence", "confidence_level", "reason", "routing_policy",
        "candidate_order", "auto_allow_excluded",
    }
    decision = explanation["routing_decision"]
    assert decision["provider"] == "exa"
    assert decision["routing_policy"] == "routing-v3"
    assert decision["reason"] == "intent_docs"
    assert decision["candidate_order"][0] == "exa"
    assert decision["auto_allow_excluded"] == ["linkup"]
    assert set(explanation["intent"]) == {"intent", "confidence", "signals"}
    assert explanation["intent"]["intent"] == "docs"
    assert explanation["intent"]["confidence"] == decision["confidence"]
    assert explanation["intent"]["signals"]
    assert explanation["first_provider_rules"] == {
        "academic": "exa",
        "docs": "exa",
        "security": "serper",
        "shopping": "serper",
    }
    assert explanation["measured_order"] == ["brave", "serper", "exa", "tavily"]
    assert explanation["query_analysis"]["routing_class"] == "docs"
    assert "parallel" not in explanation["available_providers"]
    assert "linkup" not in explanation["available_providers"]
    assert {"brave", "serper", "exa", "tavily", "you", "firecrawl"} <= set(explanation["available_providers"])
    assert "intent_breakdown" not in explanation
    json.dumps(explanation)


def test_explain_routing_with_nothing_configured_reports_the_fallback_provider():
    explanation = search.explain_routing(QUERY["general"], _config(()))

    assert explanation["routing_decision"]["reason"] == "no_available_providers"
    assert explanation["routing_decision"]["candidate_order"] == []
    assert explanation["available_providers"] == []


# --- quality report and intent reranker use the intent ---------------------------------------


DOCS_PAGE = {
    "title": "asyncio.gather - Python documentation",
    "url": "https://docs.python.org/3/library/asyncio-task.html",
    "snippet": "Run awaitable objects concurrently.",
}
BLOG_POST = {
    "title": "Asyncio gather explained",
    "url": "https://medium.com/@someone/asyncio-gather",
    "snippet": "A walkthrough of gather.",
}


def _docs_search(monkeypatch, **kwargs):
    monkeypatch.setattr(
        providers,
        "search_exa",
        lambda query, api_key, max_results=5, **_kw: {
            "provider": "exa", "query": query, "results": [dict(BLOG_POST), dict(DOCS_PAGE)],
        },
    )
    config = _config(("exa", "brave"))
    return search.run_search_request(query=QUERY["docs"], no_cache=True, config=config, **kwargs)


def test_the_intent_reranker_applies_the_docs_rules_to_a_docs_query(monkeypatch):
    result = _docs_search(monkeypatch)

    assert result["provider"] == "exa"
    assert [item["url"] for item in result["results"]] == [DOCS_PAGE["url"], BLOG_POST["url"]]
    assert result["metadata"]["intent_rerank"]["routing_class"] == "docs"
    assert result["metadata"]["intent_rerank"]["reranked"] is True


def test_the_quality_report_uses_the_docs_intent(monkeypatch):
    result = _docs_search(monkeypatch, quality_report=True)
    report = result["quality_report"]

    assert report["routing_class"] == "docs"
    assert report["routing_policy"] == "routing-v3"
    assert report["selected_provider"] == "exa"
    assert report["routing_reason"] == "intent_docs"
    assert report["authority_signals"]["rules_applied"] is True
    assert report["authority_signals"]["canonical_top_result"] is True
    assert report["authority_signals"]["canonical_domain_hits"] == ["docs.python.org"]
    assert report["authority_signals"]["demoted_domain_hits"] == ["medium.com"]
    assert report["scores"] == {}
    assert "adaptive_adjustments" not in report


def test_a_general_query_gets_no_authority_rules(monkeypatch):
    monkeypatch.setattr(
        providers,
        "search_brave",
        lambda query, api_key, max_results=5, **_kw: {
            "provider": "brave", "query": query, "results": [dict(BLOG_POST), dict(DOCS_PAGE)],
        },
    )

    result = search.run_search_request(
        query=QUERY["general"], no_cache=True, quality_report=True, config=_config(("brave",))
    )

    assert result["provider"] == "brave"
    assert [item["url"] for item in result["results"]] == [BLOG_POST["url"], DOCS_PAGE["url"]]
    assert result["quality_report"]["routing_class"] == "general"
    assert result["quality_report"]["authority_signals"]["rules_applied"] is False


def test_authority_rules_are_keyed_by_intent():
    assert set(quality.CANONICAL_DOMAIN_RULES) == {"docs", "security"}
    assert set(quality.CANONICAL_DOMAIN_RULES) <= set(INTENTS)
    for stale in ("official_docs", "security_advisory", "official_vendor_release", "policy_pdf"):
        assert stale not in quality.CANONICAL_DOMAIN_RULES
    assert not hasattr(quality, "_choose_tie_winner")


def test_the_removed_heuristic_router_is_gone():
    for name in ("QueryAnalyzer", "ROUTING_CLASS_RULES", "DEFAULT_ROUTING_CLASS", "MULTILINGUAL_ROUTING_CLASS"):
        assert not hasattr(routing, name), name
    assert not hasattr(search, "QueryAnalyzer")


# --- routing-v3 receipts reach the operator journal --------------------------------------------


def test_the_operator_privacy_check_accepts_the_routing_v3_policy_revision():
    for revision in ("routing-v3", "routing-v2"):
        assert_operator_payload_safe({"routing_receipt": {"policy_revision": revision}})


def test_an_auto_routed_search_writes_an_operator_receipt(tmp_path, monkeypatch):
    monkeypatch.setitem(
        search.SEARCH_DISPATCH,
        "brave",
        lambda *_a, **_k: {
            "provider": "brave",
            "query": "q",
            "results": [{"title": "t", "url": "https://one.example/a", "snippet": "s"}],
        },
    )
    config = _config(("brave",), provider_priority=["brave"])
    config["v3"] = {"state_path": str(tmp_path / "state.sqlite3"), "cache_dir": str(tmp_path)}
    request = legacy_request_to_v3(
        Capability.SEARCH, {"query": QUERY["general"], "provider": "auto", "count": 3}, request_id="receipt"
    )

    execution = search.execute_v3_request(request, search._search_adapter(), config)

    assert execution.response.routing_receipt["policy_revision"] == "routing-v3"
    journal = list(tmp_path.rglob("receipts.jsonl"))
    assert len(journal) == 1
    assert '"routing-v3"' in journal[0].read_text(encoding="utf-8")


def test_the_config_module_still_exports_the_migration_helper():
    assert config_module._replace_pre_5_default_priority(list(PRE_5_DEFAULT_PROVIDER_PRIORITY)) == list(
        DEFAULT_PROVIDER_PRIORITY
    )
    assert config_module._replace_pre_5_default_priority(["serper"]) == ["serper"]
