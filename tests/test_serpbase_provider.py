from wsp_core import providers
from wsp_core import http_client, result_redirects
from wsp_core import config as config_module
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

import pytest

from wsp_core import search, routing as routing_module

SEARCH_PATH = Path(__file__).resolve().parents[1] / "search.py"

auto_route_provider = search.auto_route_provider
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
        with mock.patch.object(providers, "make_request", return_value=fake_response) as mock_request:
            result = providers.search_serpbase(
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
        self.assertEqual(body["device"], "mobile")

    def test_search_serpbase_raises_on_business_error(self):
        with mock.patch.object(providers, "make_request", return_value={"status": 1020, "message": "insufficient credits"}):
            with self.assertRaises(search.ProviderRequestError) as ctx:
                providers.search_serpbase("query", "serpbase-test-key-12345")
        self.assertIn("provider status 1020", str(ctx.exception))
        self.assertNotIn("insufficient credits", str(ctx.exception))

    def test_auto_router_excludes_serpbase_and_querit_when_auto_allow_false(self):
        config = config_module._deepcopy_default_config()
        config["auto_routing"]["provider_priority"] = ["serpbase", "querit", "serper"]
        config["auto_routing"]["auto_allow"] = {"serpbase": False, "querit": False}
        env = {
            "SERPBASE_API_KEY": "serpbase-test-key-12345",
            "QUERIT_API_KEY": "querit-test-key-12345",
            "SERPER_API_KEY": "serper-test-key-12345",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            routing = auto_route_provider("latest iphone price today", config)

        self.assertEqual(routing["provider"], "serper")
        self.assertEqual(routing["candidate_order"], ["serper"])
        self.assertIn("serpbase", routing["auto_allow_excluded"])
        self.assertIn("querit", routing["auto_allow_excluded"])

    def test_explicit_serpbase_key_still_works_when_auto_allow_false(self):
        config = config_module._deepcopy_default_config()
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
        with mock.patch.object(routing_module, "get_api_key", return_value="dummy-key"):
            explanation = search.explain_routing("best price sony wh-1000xm6 Austria", config)
        excluded = explanation["routing_decision"]["auto_allow_excluded"]
        self.assertIn("serpbase", excluded)
        self.assertIn("querit", excluded)
        self.assertNotIn("serpbase", explanation["available_providers"])
        self.assertNotIn("querit", explanation["available_providers"])
        self.assertEqual(explanation["intent"]["intent"], "shopping")

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


def _recorded_shape(name):
    # Live 2026-10-30 response shapes; tokens, session IDs and text sanitised.
    return json.loads((Path(__file__).parent / "fixtures" / name).read_text())


def test_mobile_recorded_shape_keeps_six_sources_and_drops_two_goto():
    with mock.patch.object(providers, "make_request", return_value=_recorded_shape("serpbase_mobile.json")) as request:
        answer = providers.search_serpbase("python 3.14", "fake", max_results=8)
    assert request.call_args.args[2]["device"] == "mobile"
    result_redirects.clean_result_redirects(answer, config_module._deepcopy_default_config())
    assert len(answer["results"]) == 6
    assert answer["metadata"]["redirects_dropped"] == 2
    assert all("google.com" not in item["url"] for item in answer["results"])


def test_desktop_breadcrumb_shape_prefers_canonical_never_provenance():
    with mock.patch.object(providers, "make_request", return_value=_recorded_shape("serpbase_desktop.json")) as request:
        answer = providers.search_serpbase("python 3.14", "fake", device="desktop")
    assert request.call_args.args[2]["device"] == "desktop"
    first, second, provenance_only = answer["results"]
    assert first["url"] == "https://github.com/python/cpython/blob/3.14/Doc/whatsnew/3.14.rst"
    assert first["source_url"].startswith("https://www.google.com/search")
    # Do not guess repairs or claim to detect 404s; mobile avoids this upstream shape.
    assert second["url"] == "https://docs.python.org/whatsnew/3.14.html"
    assert provenance_only["url"] == ""


@pytest.mark.parametrize("device", ["tablet", "", None, [], 1])
def test_device_config_is_validated(device):
    cfg = config_module._deepcopy_default_config()
    cfg["serpbase"]["device"] = device
    with pytest.raises(ValueError, match="serpbase.device"):
        config_module._validate_runtime_config(cfg)


@pytest.mark.parametrize("device", ["mobile", "desktop"])
def test_device_config_accepts_supported_devices(device):
    cfg = config_module._deepcopy_default_config()
    cfg["serpbase"]["device"] = device
    assert config_module._validate_runtime_config(cfg)["serpbase"]["device"] == device


def test_serpbase_wire_request_uses_wsp_user_agent():
    # Exercise make_request, not a mock of it: urllib's default UA gets blocked.
    response = HTTPError("https://api.serpbase.dev/google/search", 200, "OK", {}, io.BytesIO(b'{"status":0,"organic":[]}'))
    with mock.patch.object(http_client, "urlopen", return_value=response) as open_request:
        providers.search_serpbase("fixture query", "fake")
    request = open_request.call_args.args[0]
    assert request.get_header("User-agent") == http_client.DEFAULT_USER_AGENT
    assert not request.get_header("User-agent").startswith("Python-urllib")
    assert json.loads(request.data)["device"] == "mobile"


def test_device_is_dispatched_and_has_its_own_cache_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("SERPBASE_API_KEY", "fake-serpbase-test-key")
    cfg = config_module._deepcopy_default_config()
    cfg["v3"] = {"cache_dir": str(tmp_path), "state_path": str(tmp_path / "state.sqlite3"),
                 "operator_receipt_journal": False}
    cfg["bounded_context"]["cache_root"] = str(tmp_path)
    devices = []

    def fake_search(**kwargs):
        devices.append(kwargs["device"])
        return {"provider": "serpbase", "query": kwargs["query"], "results": [{
            "title": "Python", "url": "https://www.python.org/", "snippet": "Python reference",
        }]}

    monkeypatch.setattr(providers, "search_serpbase", fake_search)
    first = search.run_search_request(query="python reference", provider="serpbase", config=cfg)
    cached = search.run_search_request(query="python reference", provider="serpbase", config=cfg)
    assert first["results"] and cached["cached"] and devices == ["mobile"]
    cfg["serpbase"]["device"] = "desktop"
    desktop = search.run_search_request(query="python reference", provider="serpbase", config=cfg)
    assert not desktop["cached"] and devices == ["mobile", "desktop"]


if __name__ == "__main__":
    unittest.main()
