import importlib.util
from pathlib import Path
from unittest import mock

SEARCH_PATH = Path(__file__).resolve().parents[1] / "search.py"
search_spec = importlib.util.spec_from_file_location("wsp_search_routing_vnext_under_test", SEARCH_PATH)
search = importlib.util.module_from_spec(search_spec)
assert search_spec.loader is not None
search_spec.loader.exec_module(search)


def _route(query):
    config = search._deepcopy_default_config()
    with mock.patch.object(search, "get_api_key", return_value="test-key"):
        return search.QueryAnalyzer(config).route(query)


def test_default_auto_allow_blocks_unreliable_and_answer_only_providers():
    config = search._deepcopy_default_config()

    auto_allow = config["auto_routing"]["auto_allow"]

    assert auto_allow["serpbase"] is False
    assert auto_allow["querit"] is False
    assert auto_allow["brave"] is False
    assert auto_allow["kilo-perplexity"] is False
    assert auto_allow["perplexity"] is False


def test_legacy_auto_allow_config_inherits_new_guarded_provider_defaults():
    config = search._deepcopy_default_config()
    config["auto_routing"]["auto_allow"] = {"serpbase": False, "querit": False}

    validated = search._validate_runtime_config(config)

    assert validated["auto_routing"]["auto_allow"]["brave"] is False
    assert validated["auto_routing"]["auto_allow"]["kilo-perplexity"] is False
    assert validated["auto_routing"]["auto_allow"]["perplexity"] is False


def test_briefing_synthesis_overrides_docs_keywords():
    routing = _route("was sind die Unterschiede zwischen Python und Node.js")

    assert routing["analysis_summary"]["routing_class"] == "briefing_synthesis"
    assert "answer_mode_recommended" not in routing


def test_official_vendor_release_routes_to_you_before_generic_serp():
    routing = _route("latest official Mistral AI model release announcement")

    assert routing["analysis_summary"]["routing_class"] == "official_vendor_release"
    assert routing["provider"] == "you"


def test_official_docs_routes_to_exa():
    routing = _route("Claude Code hooks official docs")

    assert routing["analysis_summary"]["routing_class"] == "official_docs"
    assert routing["provider"] == "exa"


def test_finance_earnings_official_prefers_linkup_when_auto_allowed():
    config = search._deepcopy_default_config()
    config["auto_routing"]["auto_allow"]["linkup"] = True
    with mock.patch.object(search, "get_api_key", return_value="test-key"):
        routing = search.QueryAnalyzer(config).route("NVIDIA Q1 FY2027 earnings official investor relations guidance")

    assert routing["analysis_summary"]["routing_class"] == "finance_earnings_official"
    assert routing["provider"] == "linkup"


def test_policy_pdf_prefers_source_grounding_provider_when_available():
    routing = _route("EU AI Act General-Purpose AI Code of Practice official PDF")

    assert routing["analysis_summary"]["routing_class"] == "policy_pdf"
    assert routing["provider"] == "linkup"


def test_community_forum_reviews_demotes_exa():
    routing = _route("best IEM under 300 euro erfahrungen forum measurements")

    assert routing["analysis_summary"]["routing_class"] == "community_forum"
    assert routing["provider"] in {"firecrawl", "serper", "you", "tavily"}
    assert routing["provider"] != "exa"


def test_shopping_geo_signals_win_over_generic_review_terms():
    routing = _route("Sony WH-1000XM5 review Geizhals Österreich")

    assert routing["analysis_summary"]["routing_class"] == "shopping_reviews_local"
    assert routing["provider"] == "serper"


def test_generic_library_docs_stay_docs_api_not_official_docs():
    routing = _route("pydantic BaseModel docs")

    assert routing["analysis_summary"]["routing_class"] == "docs_api"


def test_plain_pdf_conversion_is_not_policy_pdf():
    routing = _route("convert pdf to docx offline tool")

    assert routing["analysis_summary"]["routing_class"] != "policy_pdf"


def test_result_reranker_promotes_canonical_vendor_and_demotes_aggregators():
    results = [
        {"title": "Mistral AI Now Summit teaser", "url": "https://youtube.com/watch?v=abc"},
        {"title": "Mistral 3 guide", "url": "https://aizolo.com/blog/mistral-ai-models-2026/"},
        {"title": "Introducing Mistral 3", "url": "https://mistral.ai/news/mistral-3"},
    ]

    reranked, metadata = search.rerank_results_for_intent(
        "latest official Mistral AI model release announcement",
        "official_vendor_release",
        results,
    )

    assert reranked[0]["url"] == "https://mistral.ai/news/mistral-3"
    assert metadata["reranked"] is True


def test_result_reranker_promotes_policy_authority_over_mirrors():
    results = [
        {"title": "AI RMF mirror", "url": "https://ai.universityofcalifornia.edu/_files/riskmanagementgenerativeai.pdf"},
        {"title": "NIST AI RMF", "url": "https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.600-1.pdf"},
    ]

    reranked, _ = search.rerank_results_for_intent("NIST AI RMF official PDF", "policy_pdf", results)

    assert reranked[0]["url"].startswith("https://nvlpubs.nist.gov/")


def test_domain_rule_does_not_substring_match_middle_of_domain():
    assert search._domain_matches_rule("docs.python.org", "docs.") is True
    assert search._domain_matches_rule("notdocs.com", "docs.") is False
    assert search._domain_matches_rule("mirror.com", "ir.") is False


def test_reddit_company_finance_query_is_not_community_query():
    routing = _route("Reddit IPO earnings revenue investor relations")

    assert routing["analysis_summary"]["routing_class"] == "finance_earnings_official"


def test_plain_database_table_query_is_not_sports_current():
    routing = _route("postgres table partitioning performance documentation")

    assert routing["analysis_summary"]["routing_class"] != "sports_current"


def test_multilingual_current_japanese_routes_to_you_not_brave_or_serper():
    routing = _route("東京 AI ニュース 今日 2026 企業 発表")

    assert routing["provider"] == "you"
    assert routing["routing_policy"] == "routing-v2"
    assert routing["analysis_summary"]["language_hint"] == "ja"
    assert "brave" in routing["auto_allow_excluded"]


def test_multilingual_arabic_routes_to_you_and_blocks_querit():
    routing = _route("أخبار الذكاء الاصطناعي اليوم 2026 السعودية تنظيم")

    assert routing["provider"] == "you"
    assert routing["analysis_summary"]["language_hint"] == "ar"
    assert "querit" in routing["auto_allow_excluded"]


def test_arxiv_academic_routes_to_exa():
    routing = _route("arXiv 2024 LLM scaling laws inference compute paper")

    assert routing["provider"] == "exa"
    assert routing["analysis_summary"]["routing_class"] == "academic_arxiv"


def test_reddit_site_query_routes_away_from_exa():
    routing = _route("site:reddit.com r/hometheater Denon X4800H user impressions HDMI issues")

    assert routing["provider"] in {"serper", "firecrawl", "tavily"}
    assert routing["provider"] != "exa"
    assert routing["analysis_summary"]["routing_class"] == "reddit_community"


def test_cve_security_does_not_route_to_firecrawl():
    routing = _route("latest OpenSSH CVE 2026 mitigation advisory official")

    assert routing["provider"] in {"serper", "exa", "linkup"}
    assert routing["provider"] != "firecrawl"
    assert routing["analysis_summary"]["routing_class"] == "security_advisory"


def test_synthesis_query_routes_to_you_without_auto_selecting_kilo():
    routing = _route("Was sind die wichtigsten Unterschiede zwischen Exa Tavily und You.com für Agenten Suche")

    assert routing["provider"] == "you"
    assert "answer_mode_recommended" not in routing
    assert "kilo-perplexity" in routing["auto_allow_excluded"]
