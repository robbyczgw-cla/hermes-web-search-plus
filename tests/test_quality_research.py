import threading
import time
import unittest
from copy import deepcopy

from wsp_core import search
from wsp_core.config import DEFAULT_CONFIG, _validate_runtime_config


class QualityReportTests(unittest.TestCase):
    def test_research_quorum_config_defaults_disable_and_bounds(self):
        config = _validate_runtime_config(deepcopy(DEFAULT_CONFIG))
        self.assertEqual(
            search._research_quorum_settings(config),
            {
                "enabled": True,
                "min_contributing_providers": 2,
                "result_target_cap": 5,
                "min_unique_domains": 3,
            },
        )

        config["quality"]["research_quorum"]["enabled"] = False
        self.assertFalse(
            search._research_quorum_settings(
                _validate_runtime_config(config)
            )["enabled"]
        )

        invalid = deepcopy(DEFAULT_CONFIG)
        invalid["quality"]["research_quorum"]["min_contributing_providers"] = 1
        with self.assertRaisesRegex(ValueError, "min_contributing_providers"):
            _validate_runtime_config(invalid)

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

    def test_research_mode_returns_after_quality_quorum_without_waiting_for_blocked_provider(self):
        slow_started = threading.Event()
        release_slow = threading.Event()
        fast_b_finished = threading.Event()

        def execute(provider):
            if provider == "slow":
                slow_started.set()
                release_slow.wait(5)
                return {"provider": provider, "results": [{"url": "https://slow.test/a"}]}
            self.assertTrue(slow_started.wait(1))
            if provider == "fast-b":
                fast_b_finished.set()
                return {"provider": provider, "results": [
                    {"url": "https://three.test/b"},
                    {"url": "https://four.test/b"},
                ]}
            self.assertTrue(fast_b_finished.wait(1))
            return {"provider": provider, "results": [
                {"url": "https://one.test/a"},
                {"url": "https://two.test/a"},
            ]}

        try:
            started = time.monotonic()
            result = search.run_research_mode(
                query="quorum returns early",
                research_providers=["slow", "fast-a", "fast-b"],
                execute_search=execute,
                extract_urls=lambda urls: {"provider": None, "results": []},
                max_results=3,
                max_extract_urls=0,
                time_budget_seconds=2,
            )
            elapsed = time.monotonic() - started
        finally:
            release_slow.set()

        # fast-b completes before fast-a, but the public merge order remains
        # submission order. The blocked daemon task is explicitly preempted.
        self.assertLess(elapsed, 1.0)
        self.assertEqual(result["routing"]["providers_queried"], ["fast-a", "fast-b"])
        self.assertEqual([item["url"] for item in result["results"]], [
            "https://one.test/a",
            "https://two.test/a",
            "https://three.test/b",
        ])
        self.assertIn(
            {"provider": "slow", "error": "preempted_after_quorum"},
            result["routing"]["provider_errors"],
        )
        self.assertTrue(result["metadata"]["research_quorum"]["triggered"])

    def test_research_mode_does_not_preempt_for_one_successful_provider(self):
        result = search.run_research_mode(
            query="one provider cannot form a quorum",
            research_providers=["only"],
            execute_search=lambda provider: {
                "provider": provider,
                "results": [{"url": "https://one.test/a"}],
            },
            extract_urls=lambda urls: {"provider": None, "results": []},
            max_results=1,
            max_extract_urls=0,
        )

        self.assertEqual(result["routing"]["providers_queried"], ["only"])
        self.assertEqual(result["routing"]["provider_errors"], [])
        self.assertFalse(result["metadata"]["research_quorum"]["triggered"])

    def test_research_mode_does_not_preempt_when_domains_are_not_diverse(self):
        def execute(provider):
            if provider == "slow":
                time.sleep(1)
            return {"provider": provider, "results": [
                {"url": f"https://same.test/{provider}/a"},
                {"url": f"https://same.test/{provider}/b"},
                {"url": f"https://same.test/{provider}/c"},
            ]}

        result = search.run_research_mode(
            query="poor diversity cannot form a quorum",
            research_providers=["fast-a", "fast-b", "slow"],
            execute_search=execute,
            extract_urls=lambda urls: {"provider": None, "results": []},
            max_results=3,
            max_extract_urls=0,
            time_budget_seconds=0.1,
        )

        self.assertNotIn(
            {"provider": "slow", "error": "preempted_after_quorum"},
            result["routing"]["provider_errors"],
        )
        self.assertIn(
            {"provider": "slow", "error": "timed out: research time budget exhausted"},
            result["routing"]["provider_errors"],
        )
        self.assertFalse(result["metadata"]["research_quorum"]["triggered"])

    def test_research_mode_waits_for_small_result_sets_to_preserve_recall(self):
        release_slow = threading.Event()
        done = threading.Event()
        outcome = {}

        def execute(provider):
            if provider == "slow":
                release_slow.wait(1)
                return {"provider": provider, "results": [
                    {"url": "https://three.test/a"},
                    {"url": "https://four.test/a"},
                    {"url": "https://five.test/a"},
                ]}
            return {"provider": provider, "results": [{"url": f"https://{provider}.test/a"}]}

        def run():
            try:
                outcome["result"] = search.run_research_mode(
                    query="small result sets keep recall",
                    research_providers=["fast-a", "fast-b", "slow"],
                    execute_search=execute,
                    extract_urls=lambda urls: {"provider": None, "results": []},
                    max_results=5,
                    max_extract_urls=0,
                )
            finally:
                done.set()

        runner = threading.Thread(target=run)
        runner.start()
        try:
            self.assertFalse(done.wait(0.1))
        finally:
            release_slow.set()
        self.assertTrue(done.wait(1))
        runner.join(1)

        result = outcome["result"]
        self.assertEqual(len(result["results"]), 5)
        self.assertEqual(result["routing"]["provider_errors"], [])
        self.assertFalse(result["metadata"]["research_quorum"]["triggered"])

    def test_research_mode_provider_errors_keep_submission_order(self):
        delays = {"first": 0.03, "second": 0.01}

        def execute(provider):
            time.sleep(delays[provider])
            raise RuntimeError(f"{provider} failed")

        result = search.run_research_mode(
            query="stable diagnostics",
            research_providers=["first", "second"],
            execute_search=execute,
            extract_urls=lambda urls: {"provider": None, "results": []},
            max_results=3,
            max_extract_urls=0,
        )

        self.assertEqual(
            [item["provider"] for item in result["routing"]["provider_errors"]],
            ["first", "second"],
        )


if __name__ == "__main__":
    unittest.main()
