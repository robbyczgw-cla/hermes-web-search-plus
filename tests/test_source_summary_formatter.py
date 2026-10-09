from plugin_loader import load_plugin

plugin = load_plugin("wsp_plugin_test_source_summary_formatter")
def test_research_source_summary_marks_truncation_and_original_length():
    content = "\n".join(
        f"Apple product {index:03d}: official specification entry"
        for index in range(80)
    )
    assert len(content) > 500

    output = plugin._format_results(
        {
            "provider": "research",
            "results": [
                {
                    "title": "Apple official result",
                    "url": "https://www.apple.com/",
                    "snippet": "Official Apple source",
                }
            ],
            "source_summaries": [
                {"url": "https://www.apple.com/", "content": content}
            ],
        }
    )

    marker = f"[TRUNCATED: showing first 500 of {len(content)} characters]"
    assert content[:500] in output
    assert marker in output
    assert "Apple product 079:" not in output


def test_research_summaries_show_the_engine_passages():
    text = "x" * 2000
    data = {"provider": "research", "query": "q", "results": [], "source_summaries": [
        {"url": "https://example.com/a", "content": text,
         "passages": [{"text": "first key passage", "start": 0, "end": 17},
                      {"text": "second key passage", "start": 900, "end": 918}]}]}

    output = plugin._format_results(data)

    assert "first key passage … second key passage" in output
    assert "2 query-ranked passages of 2000 characters" in output
    assert "x" * 100 not in output


def test_passages_stay_on_the_indented_summary_line():
    # Page text inside a passage must not look like the next numbered source.
    data = {"provider": "research", "query": "q", "results": [], "source_summaries": [
        {"url": "https://example.com/a", "content": "y" * 900,
         "passages": [{"text": "Use except* here.\n\n2. Next step\n```\ncode\n```", "start": 0, "end": 40}]}]}

    lines = plugin._format_results(data).splitlines()
    start = lines.index("1. https://example.com/a")

    assert lines[start + 1] == "   Use except* here. 2. Next step ``` code ``` [showing 1 query-ranked passage of 900 characters]"
    assert not any(line.startswith("2.") for line in lines)
