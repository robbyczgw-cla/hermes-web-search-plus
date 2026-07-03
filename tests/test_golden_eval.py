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

        self.assertEqual(len(rows), 8)
        self.assertTrue(all(row["status"] == "ok" for row in rows))
        self.assertEqual(rows[0]["top_domain"], "github.com")

        live_categories = {case["category"] for case in golden_eval.load_golden_queries()}
        snapshot_categories = {snapshot["category"] for snapshot in snapshots}
        self.assertEqual(snapshot_categories, live_categories)

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


class SnapshotContractTests(unittest.TestCase):
    def test_all_fixture_payloads_match_output_contract(self):
        fixture_path = Path(__file__).parent / "fixtures" / "golden_snapshots.json"
        snapshots = golden_eval.load_snapshot_fixtures(fixture_path)

        self.assertEqual(len(snapshots), 8)
        for snapshot in snapshots:
            issues = golden_eval.validate_snapshot_payload(snapshot["payload"])
            self.assertEqual(issues, [], f"{snapshot['id']}: {issues}")

    def test_contract_rejects_unknown_top_level_field(self):
        payload = {
            "results": [{"title": "t", "url": "https://example.com"}],
            "latency_ms": 1234,
        }

        issues = golden_eval.validate_snapshot_payload(payload)

        self.assertTrue(any("unknown field 'latency_ms'" in issue for issue in issues))

    def test_contract_rejects_bad_result_shapes(self):
        payload = {
            "results": [
                {"title": "missing url"},
                {"title": 42, "url": "https://example.com"},
                {"title": "t", "url": "https://example.com", "score": "high"},
                {"title": "t", "url": "https://example.com", "tracking_id": "abc"},
            ],
        }

        issues = "\n".join(golden_eval.validate_snapshot_payload(payload))

        self.assertIn("results[0] is missing required field 'url'", issues)
        self.assertIn("results[1] field 'title' must be str", issues)
        self.assertIn("results[2] optional field 'score' has wrong type", issues)
        self.assertIn("results[3] has unknown field 'tracking_id'", issues)

    def test_contract_requires_results_list(self):
        issues = golden_eval.validate_snapshot_payload({"provider": "replay"})

        self.assertTrue(any("missing required field 'results'" in issue for issue in issues))


class SnapshotRecorderTests(unittest.TestCase):
    def test_sanitize_payload_strips_volatile_fields_and_truncates(self):
        payload = {
            "provider": "linkup",
            "results": [
                {
                    "title": "Long result",
                    "url": "https://example.com/a",
                    "snippet": "x" * 900,
                    "score": 0.91,
                    "favicon": "https://example.com/favicon.ico",
                }
            ],
            "source_summaries": [{"url": "https://example.com/a", "content": "y" * 5000}],
            "answer": "z" * 2000,
            "cached": True,
            "cache_age_seconds": 42,
            "deduplicated": False,
            "routing": {"provider": "linkup", "auto_routed": True},
            "metadata": {"dedup_count": 1, "freshness": {"requested": "week"}},
            "quality_report": {"duplicate_count": 1, "domains": ["example.com"]},
        }

        sanitized = golden_eval.sanitize_payload_for_snapshot(payload)

        self.assertEqual(golden_eval.validate_snapshot_payload(sanitized), [])
        for volatile in ("cached", "cache_age_seconds", "deduplicated", "routing", "metadata"):
            self.assertNotIn(volatile, sanitized)
        self.assertNotIn("favicon", sanitized["results"][0])
        self.assertLessEqual(len(sanitized["results"][0]["snippet"]), 300)
        self.assertLessEqual(len(sanitized["source_summaries"][0]["content"]), 1200)
        self.assertLessEqual(len(sanitized["answer"]), 600)
        self.assertEqual(sanitized["quality_report"], {"duplicate_count": 1})

    def test_default_snapshot_expectations_are_derived_from_payload(self):
        expect = golden_eval.default_snapshot_expectations({
            "results": [
                {"title": "a", "url": "https://example.com/a"},
                {"title": "b", "url": "https://example.org/b"},
            ],
        })

        self.assertEqual(expect["min_results"], 2)
        self.assertEqual(expect["min_domain_count"], 2)
        self.assertEqual(expect["max_duplicate_count"], 0)

    def test_record_snapshots_builds_contract_clean_snapshots(self):
        def fake_run(cmd, capture_output, text, timeout, env):
            class Result:
                returncode = 0
                stdout = json.dumps({
                    "provider": "linkup",
                    "results": [{"title": "T", "url": "https://example.com", "snippet": "s", "score": 0.9}],
                    "cached": False,
                    "routing": {"provider": "linkup"},
                    "quality_report": {"duplicate_count": 0},
                })
                stderr = ""
            return Result()

        with mock.patch("scripts.golden_eval.subprocess.run", side_effect=fake_run):
            snapshots, errors = golden_eval.record_snapshots(
                cases=[{"id": "q1", "category": "hifi_product", "query": "turntables"}],
                script_path=Path("search.py"),
                max_results=4,
                timeout_seconds=10,
                env={},
            )

        self.assertEqual(errors, [])
        self.assertEqual(len(snapshots), 1)
        snapshot = snapshots[0]
        self.assertEqual(snapshot["id"], "q1")
        self.assertEqual(snapshot["category"], "hifi_product")
        self.assertEqual(golden_eval.validate_snapshot_payload(snapshot["payload"]), [])
        self.assertNotIn("routing", snapshot["payload"])
        self.assertIn("expect", snapshot)

    def test_record_snapshots_reports_failed_cases(self):
        def fake_run(cmd, capture_output, text, timeout, env):
            class Result:
                returncode = 1
                stdout = json.dumps({"error": "All providers failed", "results": []})
                stderr = "boom"
            return Result()

        with mock.patch("scripts.golden_eval.subprocess.run", side_effect=fake_run):
            snapshots, errors = golden_eval.record_snapshots(
                cases=[{"id": "q1", "category": "academic", "query": "papers"}],
                script_path=Path("search.py"),
                max_results=4,
                timeout_seconds=10,
                env={},
            )

        self.assertEqual(snapshots, [])
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["id"], "q1")
        self.assertIn("All providers failed", errors[0]["error"])

    def test_record_mode_refuses_canonical_fixture_and_writes_elsewhere(self):
        with mock.patch("sys.argv", [
            "golden_eval.py", "--record", "--record-output", "tests/fixtures/golden_snapshots.json",
        ]):
            self.assertEqual(golden_eval.main(), 2)

        snapshot = {
            "id": "q1",
            "category": "hifi_product",
            "query": "turntables",
            "payload": {"provider": "replay", "results": [{"title": "T", "url": "https://example.com"}]},
            "expect": {"min_results": 1},
        }
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "golden_snapshots.recorded.json"
            with mock.patch("scripts.golden_eval.record_snapshots", return_value=([snapshot], [])):
                with mock.patch("sys.argv", ["golden_eval.py", "--record", "--record-output", str(out)]):
                    self.assertEqual(golden_eval.main(), 0)

            data = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["id"], "q1")


if __name__ == "__main__":
    unittest.main()
