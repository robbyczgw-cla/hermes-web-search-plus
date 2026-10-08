"""Long provider snippets are cut for display; the payload keeps them whole.

Exa highlights and Tavily content run to several thousand characters per
result. In a judged sample the first 1200 characters stayed useful for every
result at about half the tokens, while query-ranked passages lost much more.
"""

from plugin_loader import load_plugin

plugin = load_plugin("wsp_plugin_test_snippet_display_cap")


def _output(snippet):
    return plugin._format_results({"provider": "exa", "query": "q", "results": [
        {"title": "T", "url": "https://example.com/a", "snippet": snippet}]})


def test_long_snippet_is_cut_at_a_word_boundary_with_a_marker():
    snippet = ("word " * 400).strip()  # 1999 characters
    line = _output(snippet).splitlines()[-1]

    assert line.startswith("   word word")
    assert line.endswith(f"… [TRUNCATED: showing first 1199 of {len(snippet)} characters]")
    assert len(line) < 1300


def test_snippet_at_the_limit_prints_unchanged():
    snippet = "line one\nline two " + "x" * (1200 - 18)
    assert len(snippet) == 1200

    assert f"   {snippet}" in _output(snippet)


def test_the_payload_snippet_is_not_modified():
    result = {"title": "T", "url": "https://example.com/a", "snippet": "y " * 1000}
    plugin._format_results({"provider": "exa", "results": [result]})

    assert result["snippet"] == "y " * 1000
