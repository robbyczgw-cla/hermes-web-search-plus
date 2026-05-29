import threading
import unittest

import search


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


if __name__ == "__main__":
    unittest.main()
