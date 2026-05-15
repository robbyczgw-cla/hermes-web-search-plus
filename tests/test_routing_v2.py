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


def test_answer_synthesis_overrides_docs_keywords():
    routing = _route("was sind die Unterschiede zwischen Python und Node.js")

    assert routing["analysis_summary"]["routing_class"] == "answer_synthesis"
    assert routing["answer_mode_recommended"] is True


def test_reddit_company_finance_query_is_not_community_query():
    routing = _route("Reddit IPO earnings revenue investor relations")

    assert routing["analysis_summary"]["routing_class"] == "finance_ir"


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
    assert routing["analysis_summary"]["routing_class"] == "cve_security"


def test_answer_synthesis_recommends_answer_mode_without_auto_selecting_kilo():
    routing = _route("Was sind die wichtigsten Unterschiede zwischen Exa Tavily und You.com für Agenten Suche")

    assert routing["provider"] == "you"
    assert routing["answer_mode_recommended"] is True
    assert "kilo-perplexity" in routing["auto_allow_excluded"]
