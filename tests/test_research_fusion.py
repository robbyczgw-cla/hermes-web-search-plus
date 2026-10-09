"""Research mode fuses provider rankings (RRF) instead of concatenating them."""

from wsp_core import research


def _results(*urls):
    return {"results": [{"title": u, "url": f"https://{u}.example/x", "snippet": u} for u in urls]}


def test_rrf_interleaves_providers_and_ranks_agreement_first():
    fused, duplicates = research.fuse_results(
        [("alpha", _results("a", "b", "c")), ("beta", _results("d", "b", "e"))], max_results=5
    )

    assert [item["title"] for item in fused] == ["b", "a", "d", "c", "e"]
    assert duplicates == 1


def test_a_page_found_by_several_providers_keeps_its_best_copy_and_lists_them():
    fused, _ = research.fuse_results(
        [("alpha", _results("a", "b")), ("beta", _results("b"))], max_results=5
    )

    first = fused[0]
    assert first["title"] == "b"
    assert first["provider"] == "beta"  # rank 1 there, rank 2 at alpha
    assert first["providers"] == ["alpha", "beta"]


def test_url_variants_count_as_one_page():
    fused, duplicates = research.fuse_results(
        [
            ("alpha", {"results": [{"title": "x", "url": "https://www.site.example/p/"}]}),
            ("beta", {"results": [{"title": "x", "url": "https://site.example/p?utm_source=feed"}]}),
        ],
        max_results=5,
    )

    assert len(fused) == 1 and duplicates == 1


def test_research_mode_no_longer_returns_only_the_first_providers_top_results():
    calls = {"alpha": _results("a1", "a2", "a3", "a4", "a5"), "beta": _results("b1", "b2", "b3")}

    payload = research.run_research_mode(
        query="q",
        research_providers=["alpha", "beta"],
        execute_search=lambda name: calls[name],
        extract_urls=lambda urls: {"provider": None, "results": []},
        max_results=5,
        quorum_enabled=False,
    )

    providers = [item["provider"] for item in payload["results"]]
    assert providers[:2] == ["alpha", "beta"]
    assert "beta" in providers


LONG_PAGE = (
    "Navigation Home About Contact. " * 20
    + "TaskGroup cancels the remaining tasks when one task raises, and the errors arrive as an ExceptionGroup. "
    + "Unrelated filler paragraph about something else entirely. " * 25
    + "Use except* to handle the ExceptionGroup raised by a TaskGroup. "
    + "Footer links and copyright. " * 20
)


def test_extracted_sources_carry_query_ranked_passages():
    payload = research.run_research_mode(
        query="asyncio TaskGroup exception handling",
        research_providers=["alpha"],
        execute_search=lambda name: _results("a"),
        extract_urls=lambda urls: {"provider": "fake", "results": [{"url": urls[0], "content": LONG_PAGE}]},
        max_results=5,
        quorum_enabled=False,
    )

    passages = payload["source_summaries"][0]["passages"]
    assert 1 <= len(passages) <= 2
    assert all(len(p["text"]) <= 300 for p in passages)
    assert any("ExceptionGroup" in p["text"] for p in passages)
    assert all(LONG_PAGE[p["start"]:p["end"]].strip() for p in passages)
