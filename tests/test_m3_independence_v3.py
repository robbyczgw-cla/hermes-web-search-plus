from __future__ import annotations

from independence_v3 import analyze_source_independence


def _result(result_id, url, title, snippet=None, provider="serper"):
    value = {
        "result_id": result_id,
        "url": url,
        "canonical_url": url,
        "title": title,
        "status": "ok",
        "provenance": [
            {
                "provider": provider,
                "source_url": url,
                "retrieved_at": "2026-07-12T00:00:00Z",
            }
        ],
    }
    if snippet is not None:
        value["snippet"] = snippet
    return value


def _cluster_members(clusters):
    return sorted(sorted(item["member_result_ids"]) for item in clusters)


def test_canonical_url_variants_form_one_cluster():
    results = [
        _result("a", "HTTPS://Example.COM:443/story?utm_source=x#part", "Story"),
        _result("b", "https://example.com/story", "Story copy", provider="brave"),
    ]

    clusters, estimate = analyze_source_independence(results)

    assert _cluster_members(clusters) == [["a", "b"]]
    assert clusters[0]["providers"] == ["brave", "serper"]
    assert estimate["unique_cluster_count"] == 1
    assert estimate["result_count"] == 2


def test_deterministic_minhash_union_find_groups_near_duplicate_text():
    shared = (
        "Web Search Plus version three introduces deterministic provider routing "
        "with durable circuit state and strict privacy safe receipts"
    )
    results = [
        _result("a", "https://alpha.example/news", "Release notes", shared),
        _result(
            "b",
            "https://beta.example/article",
            "Release notes mirror",
            shared + " today",
            provider="brave",
        ),
        _result(
            "c",
            "https://gamma.example/other",
            "Football scores",
            "The home team won three nil after a late second half goal",
        ),
    ]

    forward = analyze_source_independence(results)
    reverse = analyze_source_independence(list(reversed(results)))

    assert _cluster_members(forward[0]) == [["a", "b"], ["c"]]
    assert forward == reverse
    assert forward[1]["method"] == "url+snippet-minhash-v1"
    assert forward[1]["method_degraded"] is False


def test_url_only_estimate_is_real_but_explicitly_degraded():
    clusters, estimate = analyze_source_independence(
        [_result("a", "https://one.example/a", "", None)]
    )

    assert len(clusters) == 1
    assert estimate["method"] == "url-v1"
    assert estimate["method_degraded"] is True
    assert estimate["confidence"] == "low"
    assert analyze_source_independence([]) == ([], None)
