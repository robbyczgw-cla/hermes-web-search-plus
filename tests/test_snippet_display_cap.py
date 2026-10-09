"""Long provider snippets are cut for display; the payload keeps them whole.

Exa highlights and Tavily content run to several thousand characters per
result. In a judged sample the first 1200 characters stayed useful for every
result at about half the tokens, while query-ranked passages lost much more.
"""

import pytest

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


# --- where the cut falls ----------------------------------------------------

MARKER = " … [TRUNCATED: showing first "


def _shown(snippet):
    """The snippet line without its indent, split into shown text and marker numbers."""
    line = _output(snippet).splitlines()[-1]
    assert line.startswith("   ")
    text, _, tail = line[3:].partition(MARKER)
    shown, total = tail.removesuffix(" characters]").split(" of ")
    return text, int(shown), int(total)


def test_cjk_text_with_one_early_space_is_cut_at_the_limit():
    snippet = "2026年1月5日 " + "汉" * 3000

    text, shown, total = _shown(snippet)

    assert text == snippet[:1200]
    assert (shown, total) == (1200, len(snippet))


def test_a_long_url_without_spaces_is_cut_at_the_limit():
    snippet = "https://example.com/" + "a" * 3000

    text, shown, total = _shown(snippet)

    assert text == snippet[:1200]
    assert (shown, total) == (1200, len(snippet))


def test_a_word_boundary_is_used_only_if_it_keeps_half_the_limit():
    tail = " " + "y" * 1500
    keeps_half = "x" * 600 + tail  # last space at index 600
    keeps_less = "x" * 599 + tail

    text, shown, _total = _shown(keeps_half)
    assert text == "x" * 600 and shown == 600

    text, shown, _total = _shown(keeps_less)
    assert text == keeps_less[:1200] and shown == 1200


def test_a_space_at_the_limit_keeps_the_last_whole_word():
    head = "word " * 239 + "wordy"  # 1,200 characters, the last word ends at the limit
    assert len(head) == 1200
    snippet = head + " and more after the limit " * 5

    text, shown, total = _shown(snippet)

    assert text == head
    assert text.endswith(" wordy")
    assert (shown, total) == (1200, len(snippet))


def test_a_word_that_crosses_the_limit_is_left_out():
    snippet = "word " * 240 + "next " * 100  # the space is the last character inside the limit

    text, shown, _total = _shown(snippet)

    assert shown == len(text) == 1199
    assert text.endswith(" word")


def test_a_snippet_just_over_the_limit_is_shown_whole():
    snippet = ("word " * 241)[:1201]  # 1,201 characters: a cut plus marker would be longer
    line = _output(snippet).splitlines()[-1]

    assert "TRUNCATED" not in line
    assert line == f"   {snippet}"


@pytest.mark.parametrize("unit", ["word ", "汉", "ab cd ef "])
def test_a_marker_never_makes_the_line_longer_than_the_snippet(unit):
    for length in range(1201, 1400):
        snippet = (unit * length)[:length].strip()
        line = _output(snippet).splitlines()[-1][3:]
        assert len(line) <= len(snippet), length
        if "TRUNCATED" in line:
            assert len(line) < len(snippet), length


def test_the_marker_numbers_describe_what_is_shown():
    snippet = ("lorem ipsum " * 400).strip()

    text, shown, total = _shown(snippet)

    assert shown == len(text) <= 1200
    assert total == len(snippet)
    assert snippet.startswith(text)
