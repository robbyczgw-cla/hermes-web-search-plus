"""In-process search entry (run_search_request) regression coverage.

These lock down the search.py refactor that lets the Hermes plugin call the search
pipeline in-process instead of spawning a subprocess: the provider dispatch must
still resolve per-provider config, auto-routing/caching must work, and the explicit
provider error path must return a structured dict (never sys.exit) so the in-process
caller is not killed.
"""

import unittest
from unittest import mock

import search


def _canned(provider):
    return {
        "provider": provider,
        "query": "q",
        "results": [{"url": "https://example.test/a", "title": "A", "snippet": "s"}],
        "images": [],
        "answer": "",
        "metadata": {},
    }


class InProcessSearchTests(unittest.TestCase):
    def _isolate(self, stack):
        # Keep the pipeline off the real cache/cooldown/health files.
        stack.enter_context(mock.patch.object(search, "provider_in_cooldown", lambda p: (False, 0)))
        stack.enter_context(mock.patch.object(search, "cache_get", lambda **kw: None))
        stack.enter_context(mock.patch.object(search, "cache_put", lambda **kw: None))
        stack.enter_context(mock.patch.object(search, "reset_provider_health", lambda p: None))

    def test_querit_branch_resolves_provider_config(self):
        # Regression: execute_search_request must still define querit_config locally
        # after the parser/orchestration split (previously a NameError at runtime).
        import contextlib

        with contextlib.ExitStack() as stack:
            self._isolate(stack)
            stack.enter_context(mock.patch.dict("os.environ", {"QUERIT_API_KEY": "querit-test-key"}))
            seen = {}

            def fake_querit(**kwargs):
                seen.update(kwargs)
                return _canned("querit")

            stack.enter_context(mock.patch.object(search, "search_querit", fake_querit))
            result = search.run_search_request(query="multilingual realtime news", provider="querit", count=3)

        self.assertEqual(result["provider"], "querit")
        self.assertEqual(result["results"][0]["url"], "https://example.test/a")
        # The querit timeout argument is sourced from the resolved config section.
        self.assertIn("timeout", seen)

    def test_auto_routes_in_process_and_reports_routing(self):
        import contextlib

        routing = {
            "provider": "you",
            "confidence": 0.9,
            "confidence_level": "high",
            "reason": "test-route",
            "top_signals": [],
            "scores": {"you": 9.0},
            "analysis_summary": {"routing_class": "general"},
        }
        with contextlib.ExitStack() as stack:
            self._isolate(stack)
            stack.enter_context(mock.patch.dict("os.environ", {"YOU_API_KEY": "you-test-key"}))
            stack.enter_context(mock.patch.object(search, "auto_route_provider", lambda q, c: routing))
            stack.enter_context(mock.patch.object(search, "search_you", lambda **kw: _canned("you")))
            result = search.run_search_request(query="graz weather today", provider="auto", count=3)

        self.assertTrue(result["routing"]["auto_routed"])
        self.assertEqual(result["provider"], "you")
        self.assertFalse(result["cached"])

    def test_explicit_missing_key_returns_error_dict_without_exit(self):
        import contextlib

        with contextlib.ExitStack() as stack:
            self._isolate(stack)
            stack.enter_context(mock.patch.dict("os.environ", {}, clear=True))
            result = search.run_search_request(query="anything", provider="serpbase", count=1)

        self.assertEqual(result["error"], "All providers failed")
        self.assertEqual(result["provider"], "serpbase")
        self.assertTrue(result["provider_errors"])

    def test_empty_query_returns_error_dict(self):
        result = search.run_search_request(query="", provider="auto", count=3)
        self.assertEqual(result["error"], "query is required")


if __name__ == "__main__":
    unittest.main()
