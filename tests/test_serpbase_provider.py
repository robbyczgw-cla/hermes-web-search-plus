import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SEARCH_PATH = Path(__file__).resolve().parents[1] / "search.py"
search_spec = importlib.util.spec_from_file_location("wsp_search_serpbase_under_test", SEARCH_PATH)
search = importlib.util.module_from_spec(search_spec)
assert search_spec.loader is not None
search_spec.loader.exec_module(search)

QueryAnalyzer = search.QueryAnalyzer
get_api_key = search.get_api_key
validate_api_key = search.validate_api_key


class SerpBaseProviderTests(unittest.TestCase):
    def test_get_api_key_reads_serpbase_env(self):
        with mock.patch.dict(os.environ, {"SERPBASE_API_KEY": "serpbase-test-key"}, clear=False):
            self.assertEqual(get_api_key("serpbase", {}), "serpbase-test-key")

    def test_validate_api_key_accepts_serpbase(self):
        with mock.patch.dict(os.environ, {"SERPBASE_API_KEY": "serpbase-test-key-12345"}, clear=False):
            self.assertEqual(validate_api_key("serpbase", {}), "serpbase-test-key-12345")

    def test_search_serpbase_parses_organic_results_and_checks_business_status(self):
        fake_response = {
            "status": 0,
            "session_id": "sess_123",
            "organic": [
                {
                    "title": "Example result",
                    "link": "https://example.com/page?srsltid=tracking&utm_source=x",
                    "snippet": "Example snippet",
                    "rank": 1,
                    "display_link": "example.com",
                }
            ],
            "related_searches": [{"query": "related one"}, "related two"],
            "knowledge_graph": {"title": "Example KG"},
        }
        with mock.patch.object(search, "make_request", return_value=fake_response) as mock_request:
            result = search.search_serpbase(
                query="example query",
                api_key="serpbase-test-key-12345",
                max_results=5,
                country="at",
                language="de",
            )

        self.assertEqual(result["provider"], "serpbase")
        self.assertEqual(result["session_id"], "sess_123")
        self.assertEqual(result["results"][0]["title"], "Example result")
        self.assertEqual(result["results"][0]["url"], "https://example.com/page")
        self.assertEqual(result["results"][0]["snippet"], "Example snippet")
        self.assertEqual(result["related_searches"], ["related one", "related two"])

        url, headers, body = mock_request.call_args.args[:3]
        self.assertEqual(url, "https://api.serpbase.dev/google/search")
        self.assertEqual(headers["X-API-Key"], "serpbase-test-key-12345")
        self.assertEqual(body["q"], "example query")
        self.assertEqual(body["hl"], "de")
        self.assertEqual(body["gl"], "at")
        self.assertEqual(body["page"], 1)

    def test_search_serpbase_raises_on_business_error(self):
        with mock.patch.object(search, "make_request", return_value={"status": 1020, "message": "insufficient credits"}):
            with self.assertRaises(search.ProviderRequestError) as ctx:
                search.search_serpbase("query", "serpbase-test-key-12345")
        self.assertIn("SerpBase error 1020", str(ctx.exception))

    def test_auto_router_excludes_serpbase_and_querit_when_auto_allow_false(self):
        config = search._deepcopy_default_config()
        config["auto_routing"]["provider_priority"] = ["serpbase", "querit", "serper"]
        config["auto_routing"]["auto_allow"] = {"serpbase": False, "querit": False}
        analyzer = QueryAnalyzer(config)
        env = {
            "SERPBASE_API_KEY": "serpbase-test-key-12345",
            "QUERIT_API_KEY": "querit-test-key-12345",
            "SERPER_API_KEY": "serper-test-key-12345",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            routing = analyzer.route("latest iphone price today")

        self.assertEqual(routing["provider"], "serper")
        self.assertNotIn("serpbase", routing["scores"])
        self.assertNotIn("querit", routing["scores"])
        self.assertIn("serpbase", routing["auto_allow_excluded"])
        self.assertIn("querit", routing["auto_allow_excluded"])

    def test_explicit_serpbase_key_still_works_when_auto_allow_false(self):
        config = search._deepcopy_default_config()
        config["auto_routing"]["auto_allow"] = {"serpbase": False}
        with mock.patch.dict(os.environ, {"SERPBASE_API_KEY": "serpbase-test-key-12345"}, clear=False):
            self.assertEqual(validate_api_key("serpbase", config), "serpbase-test-key-12345")


    def test_explain_routing_reports_auto_allow_exclusions(self):
        config = {
            "auto_routing": {
                "provider_priority": ["serpbase", "querit", "serper"],
                "disabled_providers": [],
                "auto_allow": {"serpbase": False, "querit": False},
                "confidence_threshold": 0.3,
            }
        }
        with mock.patch.object(search, "get_api_key", return_value="dummy-key"):
            explanation = search.explain_routing("best price sony wh-1000xm6 Austria", config)
        excluded = explanation["routing_decision"]["auto_allow_excluded"]
        self.assertIn("serpbase", excluded)
        self.assertIn("querit", excluded)
        self.assertIn("serpbase_signals", explanation["intent_breakdown"])

    def test_explicit_serpbase_missing_key_hard_fails_without_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env.pop("SERPBASE_API_KEY", None)
            env["SERPER_API_KEY"] = "serper-test-key-present-but-must-not-be-used"
            env["HERMES_HOME"] = str(Path(tmp) / "empty-hermes-home")
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SEARCH_PATH),
                    "--provider",
                    "serpbase",
                    "--query",
                    "Graz weather today",
                    "--max-results",
                    "1",
                    "--no-cache",
                ],
                cwd=str(SEARCH_PATH.parent),
                env=env,
                text=True,
                capture_output=True,
                timeout=20,
            )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Missing API key for serpbase", proc.stderr)
        self.assertNotIn('"trying_next"', proc.stderr)
        self.assertNotIn('"provider": "serper"', proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
