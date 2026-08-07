import __init__ as plugin


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
