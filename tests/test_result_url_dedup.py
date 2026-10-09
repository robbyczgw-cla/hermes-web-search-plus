"""Cross-provider dedup must keep query parameters that identify the page."""

from wsp_core.quality import deduplicate_results_across_providers, normalize_result_url


def test_identifying_query_parameters_stay_distinct():
    assert normalize_result_url("https://www.youtube.com/watch?v=A") != normalize_result_url(
        "https://www.youtube.com/watch?v=B"
    )
    assert normalize_result_url("https://news.ycombinator.com/item?id=1") != normalize_result_url(
        "https://news.ycombinator.com/item?id=2"
    )


def test_tracking_parameters_scheme_www_and_order_are_ignored():
    canonical = normalize_result_url("https://example.com/post?b=2&a=1")
    for variant in (
        "http://www.example.com/post/?a=1&b=2",
        "https://example.com/post?a=1&utm_source=x&b=2&fbclid=abc",
        "https://EXAMPLE.com/post?b=2&a=1#section",
    ):
        assert normalize_result_url(variant) == canonical


def test_dedup_keeps_different_videos_and_merges_tracking_variants():
    merged, dedup_count = deduplicate_results_across_providers(
        [
            ("serper", {"results": [
                {"url": "https://www.youtube.com/watch?v=A"},
                {"url": "https://example.com/post?utm_source=serper"},
            ]}),
            ("exa", {"results": [
                {"url": "https://youtube.com/watch?v=B"},
                {"url": "https://example.com/post"},
            ]}),
        ],
        max_results=10,
    )
    assert [item["url"] for item in merged] == [
        "https://www.youtube.com/watch?v=A",
        "https://example.com/post?utm_source=serper",
        "https://youtube.com/watch?v=B",
    ]
    assert dedup_count == 1
