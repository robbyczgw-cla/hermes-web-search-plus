import unittest

import quality


class LexicalRelevanceTests(unittest.TestCase):
    def test_relevance_prefers_results_matching_query_terms(self):
        results = [
            {"title": "Unrelated gardening tips", "url": "https://example.com/garden",
             "snippet": "How to grow tomatoes and herbs in spring."},
            {"title": "Kubernetes autoscaling guide", "url": "https://example.com/k8s",
             "snippet": "Configure horizontal pod autoscaling on Kubernetes clusters."},
        ]
        scores, detail = quality.compute_lexical_relevance(
            "kubernetes horizontal pod autoscaling", results
        )

        self.assertTrue(detail["applied"])
        self.assertEqual(scores[1], 1.0)  # best match normalized to 1.0
        self.assertLess(scores[0], scores[1])

    def test_relevance_not_applied_when_no_overlap(self):
        results = [
            {"title": "clear snippet", "url": "https://a.example/1", "snippet": "clear snippet"},
            {"title": "clear snippet", "url": "https://b.example/2", "snippet": "clear snippet"},
        ]
        scores, detail = quality.compute_lexical_relevance("weather graz today", results)

        self.assertFalse(detail["applied"])
        self.assertEqual(scores, [0.0, 0.0])

    def test_relevance_handles_empty_query_and_results(self):
        self.assertEqual(quality.compute_lexical_relevance("", [{"url": "x"}]), ([0.0], {
            "applied": False, "query_terms": [], "top_relevance": 0.0, "mean_relevance": 0.0,
        }))
        scores, detail = quality.compute_lexical_relevance("anything", [])
        self.assertEqual(scores, [])
        self.assertFalse(detail["applied"])


class LexicalRerankTests(unittest.TestCase):
    def test_general_class_reorders_by_relevance(self):
        results = [
            {"title": "Cooking blog", "url": "https://blog.example/cooking",
             "snippet": "Best pasta recipes for weeknight dinners."},
            {"title": "Rust async runtime benchmarks", "url": "https://bench.example/rust-async",
             "snippet": "Comparing tokio and async-std runtime performance for Rust services."},
        ]
        reranked, meta = quality.rerank_results_for_intent(
            "rust async runtime benchmarks tokio", "general", results
        )

        self.assertTrue(meta["lexical_applied"])
        self.assertTrue(meta["reranked"])
        self.assertEqual(reranked[0]["url"], "https://bench.example/rust-async")
        self.assertIn("relevance", reranked[0])

    def test_authority_still_dominates_lexical(self):
        # The aggregator's text matches the query better, but the canonical
        # vendor source must still win on authority.
        results = [
            {"title": "Mistral 3 release deep dive analysis announcement",
             "url": "https://medium.com/blog/mistral-3-release-announcement",
             "snippet": "mistral 3 release announcement official model details and analysis"},
            {"title": "Mistral 3", "url": "https://mistral.ai/news/mistral-3", "snippet": ""},
        ]
        reranked, meta = quality.rerank_results_for_intent(
            "official mistral 3 release announcement", "official_vendor_release", results
        )

        self.assertEqual(reranked[0]["url"], "https://mistral.ai/news/mistral-3")
        self.assertTrue(meta["reranked"])

    def test_disabling_lexical_preserves_general_order(self):
        results = [
            {"title": "A", "url": "https://a.example/1", "snippet": "alpha"},
            {"title": "B matches query terms widget gadget", "url": "https://b.example/2",
             "snippet": "widget gadget query terms"},
        ]
        reranked, meta = quality.rerank_results_for_intent(
            "widget gadget", "general", results, enable_lexical=False
        )

        self.assertFalse(meta["reranked"])
        self.assertFalse(meta["lexical_applied"])
        self.assertEqual([r["url"] for r in reranked], [r["url"] for r in results])

    def test_empty_results_returns_safely(self):
        reranked, meta = quality.rerank_results_for_intent("anything", "general", [])
        self.assertEqual(reranked, [])
        self.assertFalse(meta["reranked"])

    def test_weight_below_authority_gap_keeps_tiers_separated(self):
        # Even a high lexical weight on a neutral result must not overtake a
        # boosted canonical source for a rule-backed class.
        results = [
            {"title": "exact query match security advisory ghsa test mirror",
             "url": "https://medium.com/x/ghsa-test",
             "snippet": "ghsa test security advisory exact query match"},
            {"title": "advisory", "url": "https://github.com/advisories/GHSA-test", "snippet": ""},
        ]
        reranked, _ = quality.rerank_results_for_intent(
            "ghsa test security advisory exact query match",
            "security_advisory", results, lexical_weight=5.0
        )
        self.assertEqual(reranked[0]["url"], "https://github.com/advisories/GHSA-test")


class QualityReportRelevanceTests(unittest.TestCase):
    def test_quality_report_exposes_relevance_signals(self):
        import search

        result = {
            "results": [
                {"url": "https://x.example/1", "title": "rust async benchmarks",
                 "description": "tokio runtime benchmark comparison"},
                {"url": "https://y.example/2", "title": "unrelated", "description": "cooking"},
            ],
            "metadata": {"dedup_count": 0},
        }
        report = search.build_quality_report(
            query="rust async benchmarks tokio",
            result=result,
            routing_info={"provider": "exa", "confidence_level": "high", "confidence": 0.8},
            providers_considered=["exa"],
            eligible_providers=["exa"],
            cooldown_skips=[],
            errors=[],
        )

        signals = report["relevance_signals"]
        self.assertTrue(signals["applied"])
        self.assertGreater(signals["top_relevance"], 0.0)


if __name__ == "__main__":
    unittest.main()
