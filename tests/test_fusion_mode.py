import contextlib
import io
import json
import sys
import threading
import unittest
from unittest import mock

import search


def _run_fusion_main(argv, route, provider_payloads):
    """Drive search.main() through the fusion path with mocked providers/routing."""
    captured = io.StringIO()
    with mock.patch.object(search, "get_api_key", return_value="k"), \
            mock.patch.object(search, "provider_in_cooldown", return_value=(False, 0)), \
            mock.patch.object(search, "validate_api_key", return_value="k"), \
            mock.patch.object(search, "auto_route_provider", return_value=route), \
            mock.patch.object(search, "search_serper", side_effect=lambda **k: provider_payloads["serper"]), \
            mock.patch.object(search, "search_you", side_effect=lambda **k: provider_payloads["you"]), \
            mock.patch.object(sys, "argv", argv), \
            contextlib.redirect_stdout(captured):
        search.main()
    return json.loads(captured.getvalue())


def _route(provider, routing_class):
    return {
        "provider": provider,
        "confidence": 0.8,
        "confidence_level": "high",
        "reason": "test",
        "routing_policy": "routing-v2",
        "top_signals": [],
        "scores": {"serper": 5.0, "you": 4.0},
        "auto_allow_excluded": [],
        "analysis_summary": {"routing_class": routing_class, "language_hint": "en"},
    }


class ReciprocalRankFusionTests(unittest.TestCase):
    def test_rewards_cross_provider_agreement(self):
        results_by_provider = [
            ("alpha", {"results": [
                {"url": "https://x.test/x", "title": "X", "snippet": "x"},
                {"url": "https://y.test/y", "title": "Y", "snippet": "y"},
            ]}),
            ("beta", {"results": [
                {"url": "https://z.test/z", "title": "Z", "snippet": "z"},
                {"url": "https://x.test/x", "title": "X2", "snippet": "x again"},
            ]}),
        ]

        fused, metadata = search.reciprocal_rank_fusion(results_by_provider, max_results=5)

        # x is ranked by both providers, so RRF lifts it above either provider's solo top hit.
        self.assertEqual([r["url"] for r in fused], [
            "https://x.test/x",
            "https://z.test/z",
            "https://y.test/y",
        ])
        self.assertEqual(metadata["unique_results"], 3)
        self.assertEqual(metadata["overlap_count"], 1)
        self.assertEqual(metadata["fusion_method"], "rrf")
        self.assertEqual(fused[0]["found_by"], ["alpha", "beta"])
        self.assertGreater(fused[0]["fusion_score"], fused[1]["fusion_score"])

    def test_normalizes_urls_across_providers(self):
        results_by_provider = [
            ("alpha", {"results": [{"url": "https://www.example.com/page/", "snippet": "a"}]}),
            ("beta", {"results": [{"url": "http://example.com/page", "snippet": "b"}]}),
        ]

        fused, metadata = search.reciprocal_rank_fusion(results_by_provider, max_results=5)

        self.assertEqual(len(fused), 1)
        self.assertEqual(metadata["unique_results"], 1)
        self.assertEqual(metadata["overlap_count"], 1)
        self.assertEqual(fused[0]["found_by"], ["alpha", "beta"])

    def test_keeps_richest_snippet_for_shared_url(self):
        results_by_provider = [
            ("alpha", {"results": [{"url": "https://shared.test/a", "title": "Short", "snippet": "tiny"}]}),
            ("beta", {"results": [{"url": "https://shared.test/a", "title": "Long", "snippet": "a much longer and more useful snippet"}]}),
        ]

        fused, _ = search.reciprocal_rank_fusion(results_by_provider, max_results=5)

        self.assertEqual(len(fused), 1)
        self.assertEqual(fused[0]["title"], "Long")
        self.assertEqual(fused[0]["snippet"], "a much longer and more useful snippet")
        self.assertEqual(fused[0]["found_by"], ["alpha", "beta"])

    def test_skips_results_without_url_and_respects_max_results(self):
        results_by_provider = [
            ("alpha", {"results": [
                {"url": "", "snippet": "no url"},
                {"url": "https://a.test/1", "snippet": "1"},
                {"url": "https://a.test/2", "snippet": "2"},
            ]}),
        ]

        fused, metadata = search.reciprocal_rank_fusion(results_by_provider, max_results=1)

        self.assertEqual(len(fused), 1)
        self.assertEqual(metadata["unique_results"], 2)

    def test_empty_input_returns_empty_results(self):
        fused, metadata = search.reciprocal_rank_fusion([], max_results=5)
        self.assertEqual(fused, [])
        self.assertEqual(metadata["unique_results"], 0)
        self.assertEqual(metadata["overlap_count"], 0)


class SelectFusionProvidersTests(unittest.TestCase):
    def test_prefers_primary_then_routing_priority(self):
        selected = search.select_fusion_providers(
            primary_provider="exa",
            provider_priority=["you", "serper", "exa", "firecrawl", "tavily"],
            available_providers={"exa", "serper", "tavily", "you"},
            max_providers=3,
        )
        self.assertEqual(selected, ["exa", "you", "serper"])

    def test_skips_unavailable_and_dedups(self):
        selected = search.select_fusion_providers(
            primary_provider="brave",
            provider_priority=["you", "you", "serper"],
            available_providers={"you", "serper"},
            max_providers=5,
        )
        self.assertEqual(selected, ["you", "serper"])


class RunFusionModeTests(unittest.TestCase):
    def test_queries_providers_in_parallel_and_merges(self):
        # A 2-party barrier only releases if both providers run concurrently; a
        # sequential implementation would block on the first call and time out.
        barrier = threading.Barrier(2, timeout=5)
        payloads = {
            "you": {"provider": "you", "results": [{"url": "https://a.test/1", "title": "A1", "snippet": "alpha"}]},
            "serper": {"provider": "serper", "results": [{"url": "https://b.test/2", "title": "B2", "snippet": "beta"}]},
        }

        def execute(provider):
            barrier.wait()
            return payloads[provider]

        result = search.run_fusion_mode(
            query="parallel fan-out",
            fusion_providers=["you", "serper"],
            execute_search=execute,
            max_results=5,
        )

        self.assertEqual(result["mode"], "fusion")
        self.assertEqual(result["provider"], "fusion")
        self.assertEqual(result["routing"]["providers_queried"], ["you", "serper"])
        self.assertEqual(result["routing"]["provider_errors"], [])
        self.assertEqual({r["url"] for r in result["results"]}, {"https://a.test/1", "https://b.test/2"})

    def test_records_provider_errors_and_keeps_others(self):
        def execute(provider):
            if provider == "broken":
                raise RuntimeError("boom")
            return {"provider": provider, "results": [{"url": f"https://{provider}.test/x", "snippet": "s"}]}

        result = search.run_fusion_mode(
            query="partial failure",
            fusion_providers=["you", "broken"],
            execute_search=execute,
            max_results=5,
        )

        self.assertEqual(result["routing"]["providers_queried"], ["you"])
        self.assertEqual(result["routing"]["provider_errors"], [{"provider": "broken", "error": "boom"}])
        self.assertEqual([r["url"] for r in result["results"]], ["https://you.test/x"])

    def test_drops_providers_past_time_budget(self):
        release = threading.Event()

        def execute(provider):
            if provider == "slow":
                release.wait(timeout=5)  # never completes within the budget
                return {"provider": "slow", "results": [{"url": "https://slow.test/x", "snippet": "s"}]}
            return {"provider": provider, "results": [{"url": f"https://{provider}.test/x", "snippet": "s"}]}

        try:
            result = search.run_fusion_mode(
                query="time boxed fusion",
                fusion_providers=["you", "slow"],
                execute_search=execute,
                max_results=5,
                time_budget_seconds=0.5,
            )
        finally:
            release.set()

        self.assertEqual(result["routing"]["providers_queried"], ["you"])
        self.assertEqual(
            result["routing"]["provider_errors"],
            [{"provider": "slow", "error": "skipped: fusion time budget exhausted"}],
        )
        self.assertEqual([r["url"] for r in result["results"]], ["https://you.test/x"])

    def test_no_providers_returns_well_formed_empty_payload(self):
        def execute(provider):  # pragma: no cover - should never be called
            raise AssertionError("execute_search should not run with no providers")

        result = search.run_fusion_mode(
            query="nothing configured",
            fusion_providers=[],
            execute_search=execute,
            max_results=5,
        )

        self.assertEqual(result["results"], [])
        self.assertEqual(result["routing"]["providers_queried"], [])
        self.assertEqual(result["metadata"]["overlap_count"], 0)


class ApplyDomainConstraintsTests(unittest.TestCase):
    def test_site_operator_filters_off_domain_results(self):
        results = [
            {"url": "https://www.reddit.com/r/x/1"},
            {"url": "https://dev.to/post"},
            {"url": "https://old.reddit.com/r/x/2"},
        ]
        kept, dropped = search.apply_domain_constraints(results, "site:reddit.com best llm", None, None)
        self.assertEqual(
            [r["url"] for r in kept],
            ["https://www.reddit.com/r/x/1", "https://old.reddit.com/r/x/2"],
        )
        self.assertEqual(dropped, 1)

    def test_include_domains_filter(self):
        results = [{"url": "https://arxiv.org/abs/1"}, {"url": "https://medium.com/p"}]
        kept, dropped = search.apply_domain_constraints(results, "scaling laws", ["arxiv.org"], None)
        self.assertEqual([r["url"] for r in kept], ["https://arxiv.org/abs/1"])
        self.assertEqual(dropped, 1)

    def test_exclude_domains_filter(self):
        results = [{"url": "https://reddit.com/x"}, {"url": "https://docs.python.org/3"}]
        kept, dropped = search.apply_domain_constraints(results, "asyncio taskgroup", None, ["reddit.com"])
        self.assertEqual([r["url"] for r in kept], ["https://docs.python.org/3"])
        self.assertEqual(dropped, 1)

    def test_no_constraints_passthrough(self):
        results = [{"url": "https://a.test/1"}, {"url": "https://b.test/2"}]
        kept, dropped = search.apply_domain_constraints(results, "general query", None, None)
        self.assertEqual(kept, results)
        self.assertEqual(dropped, 0)


class FusionMainPathTests(unittest.TestCase):
    def test_site_query_drops_off_domain_results(self):
        # Regression: fusion must not dilute a site: query with off-domain results
        # just because one provider ignored the operator.
        payloads = {
            "serper": {"provider": "serper", "results": [
                {"url": "https://www.reddit.com/r/LocalLLaMA/a", "title": "R1", "snippet": "x"},
                {"url": "https://reddit.com/r/LocalLLaMA/b", "title": "R2", "snippet": "y"},
            ]},
            "you": {"provider": "you", "results": [
                {"url": "https://dev.to/some-article", "title": "D", "snippet": "z"},
                {"url": "https://reddit.com/r/LocalLLaMA/c", "title": "R3", "snippet": "w"},
            ]},
        }
        out = _run_fusion_main(
            ["search.py", "--query", "site:reddit.com best local llm server",
             "--mode", "fusion", "--fusion-providers", "serper", "you",
             "--max-results", "5", "--compact"],
            route=_route("serper", "reddit_community"),
            provider_payloads=payloads,
        )

        self.assertEqual(out["provider"], "fusion")
        urls = [r["url"] for r in out["results"]]
        self.assertTrue(urls, "expected reddit results to survive the filter")
        self.assertTrue(all("reddit.com" in u for u in urls), urls)
        self.assertFalse(any("dev.to" in u for u in urls), urls)
        self.assertEqual(out["metadata"]["domain_filtered_count"], 1)

    def test_authority_rerank_runs_in_fusion_path(self):
        # Regression: fusion returned before normal search's authority rerank, letting
        # aggregators outrank official sources on release/regulatory queries.
        payloads = {
            "serper": {"provider": "serper", "results": [
                {"url": "https://medium.com/p", "title": "M", "snippet": "m"},
                {"url": "https://www.anthropic.com/news/claude", "title": "A", "snippet": "a"},
            ]},
            "you": {"provider": "you", "results": [
                {"url": "https://medium.com/p", "title": "M2", "snippet": "m2"},
                {"url": "https://anthropic.com/news/claude", "title": "A2", "snippet": "a2"},
            ]},
        }
        out = _run_fusion_main(
            ["search.py", "--query", "official Anthropic Claude release notes",
             "--mode", "fusion", "--fusion-providers", "serper", "you",
             "--max-results", "5", "--compact"],
            route=_route("you", "official_vendor_release"),
            provider_payloads=payloads,
        )

        # Raw RRF would rank medium.com first (both providers list it at rank 0);
        # the authority rerank must lift the official anthropic.com source above it.
        self.assertIn("anthropic.com", out["results"][0]["url"])
        self.assertTrue(out["metadata"]["intent_rerank"]["reranked"])


if __name__ == "__main__":
    unittest.main()
