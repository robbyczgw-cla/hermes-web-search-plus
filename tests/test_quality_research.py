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

        # Providers run concurrently, so completion/call order is not guaranteed,
        # but both must be queried and result ordering must stay deterministic.
        self.assertEqual(sorted(calls), ["linkup", "tavily"])
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

    def test_research_mode_keeps_search_results_when_extraction_fails(self):
        def execute(provider):
            return {"provider": provider, "results": [
                {"url": "https://source.test/a", "title": "A", "description": "Alpha"},
            ]}

        def extract(urls):
            raise RuntimeError("extract provider timed out")

        result = search.run_research_mode(
            query="grounded answer please",
            research_providers=["linkup"],
            execute_search=execute,
            extract_urls=extract,
            max_results=3,
            max_extract_urls=1,
        )

        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["source_summaries"], [])
        self.assertEqual(result["routing"]["extraction_provider"], None)
        self.assertEqual(result["routing"]["extraction_error"], "extract provider timed out")
        self.assertEqual(result["metadata"]["extracted_url_count"], 0)

    def test_research_mode_preserves_provider_order_when_completion_is_out_of_order(self):
        import time as _time

        def execute(provider):
            # Provider submitted first finishes last; ordering must still follow
            # submission order so deduplication stays deterministic.
            if provider == "tavily":
                _time.sleep(0.05)
            return {"provider": provider, "results": [
                {"url": f"https://{provider}.test/a", "title": provider, "description": "x"},
            ]}

        def extract(urls):
            return {"provider": None, "results": []}

        result = search.run_research_mode(
            query="ordered research",
            research_providers=["tavily", "linkup"],
            execute_search=execute,
            extract_urls=extract,
            max_results=5,
            max_extract_urls=0,
        )

        self.assertEqual(result["routing"]["providers_queried"], ["tavily", "linkup"])
        self.assertEqual([r["url"] for r in result["results"]], [
            "https://tavily.test/a",
            "https://linkup.test/a",
        ])

    def test_research_mode_respects_time_budget_between_providers_and_skips_extract(self):
        # start, submit-gate linkup, submit-gate tavily, collect linkup, extract gate
        ticks = iter([0.0, 0.0, 6.0, 6.0, 6.0])
        calls = []

        def now():
            return next(ticks)

        def execute(provider):
            calls.append(provider)
            return {"provider": provider, "results": [
                {"url": f"https://{provider}.test/a", "title": provider, "description": "Result"},
            ]}

        def extract(urls):
            raise AssertionError("extract should be skipped once budget is exhausted")

        result = search.run_research_mode(
            query="time boxed research",
            research_providers=["linkup", "tavily"],
            execute_search=execute,
            extract_urls=extract,
            max_results=5,
            max_extract_urls=1,
            time_budget_seconds=5,
            now_fn=now,
        )

        self.assertEqual(calls, ["linkup"])
        self.assertEqual(result["routing"]["provider_errors"], [{"provider": "tavily", "error": "skipped: research time budget exhausted"}])
        self.assertEqual(result["routing"]["extraction_error"], "skipped: research time budget exhausted")
        self.assertEqual(result["metadata"]["extracted_url_count"], 0)

    def test_research_mode_time_budget_bounds_slow_providers_already_running(self):
        import time as _time

        def execute(provider):
            if provider == "slowpoke":
                _time.sleep(5.0)
            return {"provider": provider, "results": [
                {"url": f"https://{provider}.test/a", "title": provider, "description": "Result"},
            ]}

        def extract(urls):
            raise AssertionError("extract should be skipped once budget is exhausted")

        start = _time.monotonic()
        result = search.run_research_mode(
            query="budget bounds completion",
            research_providers=["fast", "slowpoke"],
            execute_search=execute,
            extract_urls=extract,
            max_results=5,
            max_extract_urls=1,
            time_budget_seconds=0.5,
        )
        elapsed = _time.monotonic() - start

        # The budget must bound wall-clock time even though slowpoke was already
        # submitted; without completion-side enforcement this would take ~5s.
        self.assertLess(elapsed, 3.0)
        self.assertEqual([r["url"] for r in result["results"]], ["https://fast.test/a"])
        self.assertEqual(result["routing"]["providers_queried"], ["fast"])
        slow_errors = [e for e in result["routing"]["provider_errors"] if e["provider"] == "slowpoke"]
        self.assertEqual(slow_errors, [{"provider": "slowpoke", "error": "timed out: research time budget exhausted"}])

    def test_research_mode_time_budget_bounds_slow_extraction(self):
        import time as _time

        def execute(provider):
            return {"provider": provider, "results": [
                {"url": "https://source.test/a", "title": "A", "description": "Alpha"},
            ]}

        def extract(urls):
            _time.sleep(5.0)
            return {"provider": "linkup", "results": [{"url": u, "content": "late"} for u in urls]}

        start = _time.monotonic()
        result = search.run_research_mode(
            query="budget bounds extraction",
            research_providers=["fast"],
            execute_search=execute,
            extract_urls=extract,
            max_results=3,
            max_extract_urls=1,
            time_budget_seconds=0.5,
        )
        elapsed = _time.monotonic() - start

        self.assertLess(elapsed, 3.0)
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["source_summaries"], [])
        self.assertEqual(result["routing"]["extraction_error"], "timed out: research time budget exhausted")

    def test_research_mode_without_budget_still_waits_for_all_providers(self):
        import time as _time

        def execute(provider):
            if provider == "slow":
                _time.sleep(0.2)
            return {"provider": provider, "results": [
                {"url": f"https://{provider}.test/a", "title": provider, "description": "x"},
            ]}

        def extract(urls):
            return {"provider": None, "results": []}

        result = search.run_research_mode(
            query="no budget",
            research_providers=["slow", "fast"],
            execute_search=execute,
            extract_urls=extract,
            max_results=5,
            max_extract_urls=0,
        )

        self.assertEqual(result["routing"]["providers_queried"], ["slow", "fast"])
        self.assertEqual(result["routing"]["provider_errors"], [])


if __name__ == "__main__":
    unittest.main()
