import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import golden_eval


class GoldenEvalTests(unittest.TestCase):
    def test_load_golden_queries_has_core_categories(self):
        cases = golden_eval.load_golden_queries()
        categories = {case["category"] for case in cases}

        self.assertGreaterEqual(len(cases), 8)
        self.assertIn("hifi_product", categories)
        self.assertIn("local_realtime", categories)
        self.assertIn("tech_release", categories)
        self.assertIn("research_policy", categories)
        self.assertIn("german_realtime", categories)
        self.assertIn("reddit_community", categories)
        self.assertIn("academic", categories)
        self.assertIn("js_extraction", categories)

    def test_summarize_result_flags_failures_and_metrics(self):
        payload = {
            "provider": "research",
            "results": [
                {"url": "https://example.com/a"},
                {"url": "https://example.org/b"},
            ],
            "source_summaries": [{"url": "https://example.com/a", "content": "x" * 120}],
            "quality_report": {
                "domain_count": 2,
                "domain_diversity": 1.0,
                "duplicate_count": 1,
                "extract_recommended": False,
            },
            "metadata": {"dedup_count": 1},
        }

        row = golden_eval.summarize_result(
            case={"id": "q1", "category": "research_policy", "query": "EU AI Act"},
            mode="research",
            payload=payload,
            latency_ms=1234,
            returncode=0,
            stderr="",
        )

        self.assertEqual(row["id"], "q1")
        self.assertEqual(row["mode"], "research")
        self.assertEqual(row["provider"], "research")
        self.assertEqual(row["result_count"], 2)
        self.assertEqual(row["source_summary_count"], 1)
        self.assertEqual(row["extracted_chars"], 120)
        self.assertEqual(row["dedup_count"], 1)
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["failure_flags"], [])

    def test_summarize_result_marks_empty_and_provider_error(self):
        row = golden_eval.summarize_result(
            case={"id": "q2", "category": "tech_release", "query": "latest release"},
            mode="normal",
            payload={"error": "All providers failed", "provider": "auto", "results": []},
            latency_ms=50,
            returncode=1,
            stderr="boom",
        )

        self.assertEqual(row["status"], "error")
        self.assertIn("no_results", row["failure_flags"])
        self.assertIn("provider_error", row["failure_flags"])
        self.assertIn("nonzero_exit", row["failure_flags"])

    def test_write_jsonl_and_markdown_report(self):
        rows = [
            {"id": "q1", "mode": "normal", "status": "ok", "failure_flags": [], "latency_ms": 100, "provider": "linkup", "result_count": 3, "source_summary_count": 0, "domain_diversity": 1.0, "extract_recommended": False},
            {"id": "q1", "mode": "research", "status": "ok", "failure_flags": ["slow"], "latency_ms": 22000, "provider": "research", "result_count": 4, "source_summary_count": 2, "domain_diversity": 1.0, "extract_recommended": False},
        ]
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "eval.jsonl"
            report = Path(td) / "report.md"
            golden_eval.write_jsonl(rows, out)
            golden_eval.write_markdown_report(rows, report)

            lines = out.read_text().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0])["id"], "q1")
            text = report.read_text()
            self.assertIn("# web-search-plus Golden Query Evaluation", text)
            self.assertIn("normal", text)
            self.assertIn("research", text)

    def test_snapshot_fixture_quality_checks_pass_and_fail_deterministically(self):
        fixture_path = Path(__file__).parent / "fixtures" / "golden_snapshots.json"
        snapshots = golden_eval.load_snapshot_fixtures(fixture_path)
        rows = golden_eval.run_snapshot_quality(snapshots)

        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row["status"] == "ok" for row in rows))
        self.assertEqual(rows[0]["top_domain"], "github.com")

        broken = snapshots[0].copy()
        broken["payload"] = {"results": [{"url": "https://example-mirror.invalid/only"}]}
        row = golden_eval.evaluate_snapshot_quality(broken)

        self.assertEqual(row["status"], "fail")
        self.assertIn("too_few_results", row["failure_flags"])
        self.assertIn("missing_required_domain", row["failure_flags"])
        self.assertIn("top_domain_not_canonical", row["failure_flags"])
        self.assertIn("blocked_domain_present", row["failure_flags"])

    def test_snapshot_quality_recomputes_duplicates_from_result_urls(self):
        snapshot = {
            "id": "dupes",
            "category": "tech_release",
            "query": "release notes",
            "payload": {
                "results": [
                    {"url": "https://example.com/a"},
                    {"url": "https://example.com/a/"},
                    {"url": "https://example.com/b"},
                ],
                "quality_report": {"duplicate_count": 0},
            },
            "expect": {"max_duplicate_count": 0},
        }

        row = golden_eval.evaluate_snapshot_quality(snapshot)

        self.assertEqual(row["duplicate_count"], 1)
        self.assertIn("too_many_duplicates", row["failure_flags"])

    def test_snapshot_quality_counts_research_source_summary_content(self):
        snapshot = {
            "id": "research-content",
            "category": "research_policy",
            "query": "EU AI Act",
            "payload": {
                "results": [{"url": "https://ec.europa.eu/a"}],
                "source_summaries": [{"url": "https://ec.europa.eu/a", "content": "x" * 120}],
            },
            "expect": {"min_content_chars": 100},
        }

        row = golden_eval.evaluate_snapshot_quality(snapshot)

        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["content_chars"], 120)

    def test_run_case_builds_expected_commands(self):
        calls = []

        def fake_run(cmd, capture_output, text, timeout, env):
            calls.append(cmd)
            class Result:
                returncode = 0
                stdout = json.dumps({"provider": "linkup", "results": [{"url": "https://example.com"}], "quality_report": {}})
                stderr = ""
            return Result()

        with mock.patch("scripts.golden_eval.subprocess.run", side_effect=fake_run):
            rows = golden_eval.run_case(
                case={"id": "q1", "category": "hifi_product", "query": "turntables"},
                script_path=Path("search.py"),
                modes=["normal", "research"],
                max_results=3,
                research_extract_count=1,
                timeout_seconds=10,
                env={},
            )

        self.assertEqual(len(rows), 2)
        self.assertIn("--quality-report", calls[0])
        self.assertNotIn("--mode", calls[0])
        self.assertIn("--mode", calls[1])
        self.assertIn("research", calls[1])


if __name__ == "__main__":
    unittest.main()
