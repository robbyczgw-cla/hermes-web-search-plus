import unittest

import search


class QualityReportTests(unittest.TestCase):
    def test_quality_report_scores_domain_diversity_and_extract_need(self):
        result = {
            "results": [
                {"url": "https://example.com/a", "title": "A", "description": "short"},
                {"url": "https://example.com/b", "title": "B", "description": "tiny"},
                {"url": "https://news.example.org/c", "title": "C", "description": "useful enough snippet for source triage"},
            ],
            "metadata": {"dedup_count": 2},
        }
        routing = {
            "provider": "tavily",
            "confidence": 0.32,
            "confidence_level": "low",
            "reason": "low confidence test",
            "scores": {"tavily": 4.0, "exa": 3.7},
        }

        report = search.build_quality_report(
            query="explain some obscure topic",
            result=result,
            routing_info=routing,
            providers_considered=["tavily", "exa", "linkup"],
            eligible_providers=["tavily", "exa"],
            cooldown_skips=[{"provider": "linkup", "cooldown_remaining_seconds": 42}],
            errors=[{"provider": "brave", "error": "missing key"}],
        )

        self.assertEqual(report["selected_provider"], "tavily")
        self.assertEqual(report["duplicate_count"], 2)
        self.assertEqual(report["domain_count"], 2)
        self.assertAlmostEqual(report["domain_diversity"], 2 / 3)
        self.assertEqual(report["confidence"], "low")
        self.assertTrue(report["extract_recommended"])
        self.assertIn("low routing confidence", report["extract_reasons"])
        self.assertEqual(report["skipped_providers"][0]["provider"], "linkup")

    def test_quality_report_high_confidence_diverse_results_do_not_need_extract(self):
        result = {
            "results": [
                {"url": "https://a.example/1", "description": "clear snippet " * 8},
                {"url": "https://b.example/2", "description": "clear snippet " * 8},
                {"url": "https://c.example/3", "description": "clear snippet " * 8},
            ],
            "metadata": {"dedup_count": 0},
        }
        routing = {"provider": "brave", "confidence_level": "high", "confidence": 0.91, "reason": "clear"}

        report = search.build_quality_report(
            query="weather graz today",
            result=result,
            routing_info=routing,
            providers_considered=["brave"],
            eligible_providers=["brave"],
            cooldown_skips=[],
            errors=[],
        )

        self.assertFalse(report["extract_recommended"])
        self.assertEqual(report["extract_reasons"], [])

    def test_quality_report_for_forced_provider_does_not_treat_missing_confidence_as_low(self):
        result = {
            "results": [
                {"url": "https://a.example/1", "description": "clear snippet " * 8},
                {"url": "https://b.example/2", "description": "clear snippet " * 8},
                {"url": "https://c.example/3", "description": "clear snippet " * 8},
            ],
            "metadata": {"dedup_count": 0},
        }
        routing = {"auto_routed": False, "provider": "linkup"}

        report = search.build_quality_report(
            query="best turntables under 1000 euro",
            result=result,
            routing_info=routing,
            providers_considered=["linkup"],
            eligible_providers=["linkup"],
            cooldown_skips=[],
            errors=[],
        )

        self.assertEqual(report["confidence"], "unknown")
        self.assertFalse(report["extract_recommended"])
        self.assertNotIn("low routing confidence", report["extract_reasons"])


class ResearchModeTests(unittest.TestCase):
    def test_select_research_providers_prefers_primary_plus_source_providers(self):
        selected = search.select_research_providers(
            primary_provider="tavily",
            provider_priority=["tavily", "linkup", "exa", "firecrawl", "brave"],
            available_providers={"tavily", "linkup", "exa", "brave"},
            max_providers=3,
        )

        self.assertEqual(selected, ["tavily", "linkup", "exa"])

    def test_research_mode_merges_dedups_and_extracts_top_sources(self):
        provider_payloads = {
            "tavily": {"provider": "tavily", "results": [
                {"url": "https://example.com/a", "title": "A", "description": "Alpha"},
                {"url": "https://example.com/dupe", "title": "Dupe", "description": "Duplicate"},
            ]},
            "linkup": {"provider": "linkup", "results": [
                {"url": "https://example.com/dupe", "title": "Dupe 2", "description": "Duplicate again"},
                {"url": "https://other.test/b", "title": "B", "description": "Beta"},
            ]},
        }
        calls = []

        def execute(provider):
            calls.append(provider)
            return provider_payloads[provider]

        def extract(urls):
            return {"provider": "linkup", "results": [{"url": u, "content": f"content for {u}"} for u in urls]}

        result = search.run_research_mode(
            query="compare alpha beta",
            research_providers=["tavily", "linkup"],
            execute_search=execute,
            extract_urls=extract,
            max_results=5,
            max_extract_urls=2,
        )

        self.assertEqual(calls, ["tavily", "linkup"])
        self.assertEqual(result["mode"], "research")
        self.assertEqual(result["routing"]["providers_queried"], ["tavily", "linkup"])
        self.assertEqual(result["metadata"]["dedup_count"], 1)
        self.assertEqual([r["url"] for r in result["results"]], [
            "https://example.com/a",
            "https://example.com/dupe",
            "https://other.test/b",
        ])
        self.assertEqual([s["url"] for s in result["source_summaries"]], [
            "https://example.com/a",
            "https://example.com/dupe",
        ])
        self.assertEqual(result["source_summaries"][0]["content"], "content for https://example.com/a")


if __name__ == "__main__":
    unittest.main()
