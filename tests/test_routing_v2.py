from wsp_core import config as config_module
from wsp_core import quality
from unittest import mock

from wsp_core import search, routing as routing_module


def _route(query):
    config = config_module._deepcopy_default_config()
    with mock.patch.object(routing_module, "get_api_key", return_value="test-key"):
        return search.auto_route_provider(query, config)


def test_default_auto_allow_blocks_explicit_only_providers():
    config = config_module._deepcopy_default_config()

    auto_allow = config["auto_routing"]["auto_allow"]

    assert auto_allow["serpbase"] is False
    assert auto_allow["querit"] is False
    assert auto_allow.get("parallel", True) is True
    assert auto_allow["donsetch"] is False
    assert auto_allow["octen"] is False
    assert auto_allow.get("brave", True) is True
    assert set(auto_allow) == {
        "serpbase", "querit", "donsetch", "octen", "tinyfish",
    }


def test_legacy_auto_allow_config_inherits_new_guarded_provider_defaults():
    config = config_module._deepcopy_default_config()
    config["auto_routing"]["auto_allow"] = {"serpbase": False, "querit": False}

    validated = config_module._validate_runtime_config(config)

    assert validated["auto_routing"]["auto_allow"].get("brave", True) is True
    assert validated["auto_routing"]["auto_allow"].get("parallel", True) is True
    assert validated["auto_routing"]["auto_allow"]["donsetch"] is False
    assert validated["auto_routing"]["auto_allow"]["octen"] is False
    assert set(validated["auto_routing"]["auto_allow"]) == {
        "serpbase", "querit", "donsetch", "octen", "tinyfish",
    }


def test_official_docs_routes_to_exa():
    routing = _route("Claude Code hooks official docs")

    assert routing["analysis_summary"]["routing_class"] == "docs"
    assert routing["provider"] == "exa"


def test_community_forum_reviews_demotes_exa():
    routing = _route("best IEM under 300 euro erfahrungen forum measurements")

    assert routing["analysis_summary"]["routing_class"] == "community"
    assert routing["provider"] == "serper"
    assert routing["provider"] != "exa"


def test_review_terms_alone_do_not_make_a_shopping_query():
    # A model number and "review" are weak cues; shopping needs a substantial
    # one before a query leaves the Brave default for Serper.
    routing = _route("Sony WH-1000XM5 review Geizhals Österreich")

    assert routing["analysis_summary"]["routing_class"] == "general"
    assert routing["provider"] == "brave"


def test_plain_pdf_conversion_query_stays_general():
    routing = _route("convert pdf to docx offline tool")

    assert routing["analysis_summary"]["routing_class"] == "general"
    assert routing["provider"] == "brave"


def test_quality_report_exposes_authority_signals_for_canonical_classes():
    report = search.build_quality_report(
        query="CVE-2024-1 advisory",
        result={
            "results": [
                {"title": "CVE-2024-1 Detail", "url": "https://nvd.nist.gov/vuln/detail/CVE-2024-1"},
                {"title": "CVE write-up", "url": "https://medium.com/example/cve-2024-1"},
            ],
            "metadata": {"dedup_count": 0},
        },
        routing_info={
            "provider": "brave",
            "confidence_level": "high",
            "analysis_summary": {"routing_class": "security", "language_hint": "en"},
        },
        providers_considered=["brave"],
        eligible_providers=["brave"],
        cooldown_skips=[],
        errors=[],
    )

    signals = report["authority_signals"]
    assert signals["rules_applied"] is True
    assert signals["canonical_top_result"] is True
    assert "nvd.nist.gov" in signals["canonical_domain_hits"]
    assert "medium.com" in signals["demoted_domain_hits"]
    assert "adaptive_adjustments" not in report


def test_security_advisory_authority_signals_match_github_advisory_paths():
    for url in (
        "https://github.com/advisories/GHSA-test",
        "https://github.com/owner/repo/security/advisories/GHSA-test",
    ):
        signals = quality.build_authority_signals(
            "security",
            [{"url": url}],
        )

        assert signals["canonical_domain_hits"] == ["github.com"]
        assert signals["canonical_top_result"] is True


def test_security_advisory_reranker_promotes_github_advisories_over_mirrors():
    results = [
        {"title": "Mirror", "url": "https://medium.com/example/ghsa-test"},
        {"title": "GitHub Advisory", "url": "https://github.com/advisories/GHSA-test"},
    ]

    reranked, metadata = search.rerank_results_for_intent("GHSA-test", "security", results)

    assert reranked[0]["url"] == "https://github.com/advisories/GHSA-test"
    assert metadata["reranked"] is True


def test_domain_rule_does_not_substring_match_middle_of_domain():
    assert quality._domain_matches_rule("docs.python.org", "docs.") is True
    assert quality._domain_matches_rule("notdocs.com", "docs.") is False
    assert quality._domain_matches_rule("mirror.com", "ir.") is False


def test_domain_rule_rejects_lookalike_registrations():
    # A look-alike domain must not inherit the boost of the real one.
    assert quality._domain_matches_rule("openai.com.evil.example", "openai.com") is False
    assert quality._domain_matches_rule("github.community-fake.xyz", "github.com") is False
    assert quality._domain_matches_rule("openai.com", "openai.com") is True
    assert quality._domain_matches_rule("platform.openai.com", "openai.com") is True


def test_japanese_query_uses_the_default_first_provider():
    routing = _route("東京 AI ニュース 今日 2026 企業 発表")

    assert routing["provider"] == "brave"
    assert routing["routing_policy"] == "routing-v3"
    assert routing["analysis_summary"]["language_hint"] == "ja"
    assert "brave" not in routing["auto_allow_excluded"]


def test_arabic_query_uses_the_default_first_provider_and_blocks_querit():
    routing = _route("أخبار الذكاء الاصطناعي اليوم 2026 السعودية تنظيم")

    assert routing["provider"] == "brave"
    assert routing["analysis_summary"]["language_hint"] == "ar"
    assert "querit" in routing["auto_allow_excluded"]


def test_arxiv_academic_routes_to_exa():
    routing = _route("arXiv 2024 LLM scaling laws inference compute paper")

    assert routing["provider"] == "exa"
    assert routing["analysis_summary"]["routing_class"] == "academic"


def test_reddit_site_query_routes_away_from_exa():
    routing = _route("site:reddit.com r/hometheater Denon X4800H user impressions HDMI issues")

    assert routing["provider"] == "serper"
    assert routing["provider"] != "exa"
    assert routing["analysis_summary"]["routing_class"] == "community"


def test_cve_security_routes_to_serper():
    routing = _route("latest OpenSSH CVE 2026 mitigation advisory official")

    assert routing["provider"] == "serper"
    assert routing["analysis_summary"]["routing_class"] == "security"


def test_synthesis_queries_have_no_answer_mode_recommendation():
    for query in (
        "was sind die Unterschiede zwischen Python und Node.js",
        "Was sind die wichtigsten Unterschiede zwischen Exa Tavily und You.com für Agenten Suche",
    ):
        routing = _route(query)

        assert routing["provider"] == "brave"
        assert "answer_mode_recommended" not in routing
