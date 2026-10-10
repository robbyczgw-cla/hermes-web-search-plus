"""A successful search with no hits says so; non-empty output is unchanged."""

from unittest import mock

from plugin_loader import load_plugin

plugin = load_plugin("wsp_plugin_test_zero_results_notice")

NOTICE = (
    "No results found for this query. Do not invent sources or facts; "
    "try a broader query, different filters or another provider."
)


class _Ctx:
    def __init__(self):
        self.tools = {}

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs


def _search_handler():
    ctx = _Ctx()
    plugin.register(ctx)
    return ctx.tools["web_search_plus"]["handler"]


def test_explicit_provider_with_no_hits_says_so():
    data = {"provider": "brave", "query": "q", "results": []}
    with mock.patch.object(plugin, "_run_search", return_value=data):
        output = _search_handler()("q", provider="brave")

    assert output == "\n".join([plugin._UNTRUSTED_WEB_DATA_NOTICE, "[Provider: brave]", NOTICE])


def test_auto_routed_search_with_no_hits_says_so():
    data = {
        "provider": "brave", "query": "q", "results": [],
        "routing": {"auto_routed": True, "confidence_level": "medium", "reason": "intent_general"},
    }
    with mock.patch.object(plugin, "_run_search", return_value=data):
        output = _search_handler()("q")

    assert output == "\n".join([
        plugin._UNTRUSTED_WEB_DATA_NOTICE,
        "[Provider: brave | auto-routed | medium confidence | intent_general]",
        NOTICE,
    ])


def test_research_mode_with_no_hits_says_so():
    data = {
        "mode": "research", "provider": "research", "query": "q", "results": [],
        "source_summaries": [], "routing": {}, "metadata": {},
    }
    with mock.patch.object(plugin, "_run_search", return_value=data):
        output = _search_handler()("q", mode="research")

    assert output == "\n".join([plugin._UNTRUSTED_WEB_DATA_NOTICE, "[Provider: research]", NOTICE])


def test_search_error_keeps_its_own_text():
    data = {
        "error": "all providers failed", "query": "q", "results": [],
        "provider_errors": [{"provider": "brave", "error": "quota exhausted"}],
    }
    with mock.patch.object(plugin, "_run_search", return_value=data):
        output = _search_handler()("q", provider="brave")

    assert output == "Search error: all providers failed\n- brave: quota exhausted"
    assert NOTICE not in output


def test_non_empty_output_is_unchanged():
    data = {
        "provider": "brave", "query": "q",
        "results": [{"title": "T", "url": "https://a.test/1", "snippet": "s"}],
    }

    output = plugin._format_results(data)

    assert output == "\n".join([
        plugin._UNTRUSTED_WEB_DATA_NOTICE,
        "[Provider: brave]",
        "1. T",
        "   https://a.test/1",
        "   s",
    ])
    assert NOTICE not in output
