"""Every provider attempt ends for exactly one reason, and status follows it.

Before this, a research member that was still running when the quorum was
reached (or a hedge loser) was reported as ``cancelled`` with "Provider request
timed out", which made the response ``degraded`` with the warning "Execution
was limited by its attempt or time budget" although the results were complete.
Whether that happened depended on milliseconds. ``fallback_reason`` was derived
in three places and the v3 receipt disagreed with the legacy routing block.

Fake providers answer at the HTTP boundary (``http_client.urlopen``); the
SQLite state store, the attempt engine, the research quorum loop and both the
v3 response and the legacy payload are real.
"""

from __future__ import annotations

import io
import json
import time
from urllib.error import HTTPError

import pytest

from wsp_core import http_client, search
from wsp_core.config import _deepcopy_default_config
from wsp_core.contract_v3 import (
    AttemptEndReason,
    AttemptOutcome,
    Capability,
    ErrorClass,
    ErrorV3,
    ProviderAttemptV3,
    SkipReason,
)
from wsp_core.orchestrator_v3 import execute_v3_request

HOST = {"serper": "google.serper.dev", "brave": "api.search.brave.com", "exa": "api.exa.ai"}
KEYS = {"SERPER_API_KEY": "serper-test-key-123456", "BRAVE_API_KEY": "brave-test-key-1234567",
        "EXA_API_KEY": "exa-test-key-123456789"}


class _Response:
    def __init__(self, body):
        self._body = json.dumps(body).encode()
        self.status = 200
        self.headers = {"Content-Type": "application/json"}

    def read(self, *_args):
        return self._body

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def info(self):
        return self

    def get(self, name, default=None):
        return self.headers.get(name, default)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _answer(provider, count, domain_base, delay=0.0):
    def handler(url):
        if delay:
            time.sleep(delay)
        items = [
            {"title": f"{provider} {i}", "url": f"https://{domain_base}{i}.com/{provider}", "snippet": f"from {provider} {i}"}
            for i in range(count)
        ]
        if "serper" in url:
            return _Response({"organic": [{"title": i["title"], "link": i["url"], "snippet": i["snippet"]} for i in items]})
        if "brave" in url:
            return _Response({"web": {"results": [{"title": i["title"], "url": i["url"], "description": i["snippet"]} for i in items]}})
        return _Response({"results": [{"title": i["title"], "url": i["url"], "text": i["snippet"]} for i in items]})
    return handler


def _http_error(status):
    def handler(url):
        raise HTTPError(url, status, "err", {}, io.BytesIO(b'{"message":"err"}'))
    return handler


@pytest.fixture
def providers_at_http(monkeypatch, tmp_path):
    """Route every provider HTTP call to a per-host handler; nothing leaves the box."""
    for name, value in KEYS.items():
        monkeypatch.setenv(name, value)
    behaviour = {}

    def fake_urlopen(req, timeout=30):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        host = url.split("/")[2]
        handler = behaviour.get(host)
        if handler is None:
            raise HTTPError(url, 599, f"unexpected host {host}", {}, io.BytesIO(b"{}"))
        return handler(url)

    monkeypatch.setattr(http_client, "urlopen", fake_urlopen)
    # Research reads its top sources after the member search; that is not under test.
    monkeypatch.setattr(search, "extract_plus", lambda **_kw: {"provider": None, "results": []})
    monkeypatch.setattr(search, "auto_route_provider", lambda query, config: {
        "provider": "serper", "confidence": 0.9, "confidence_level": "high", "reason": "test",
        "routing_policy": "routing-v3", "scores": {"serper": 9.0}, "top_signals": [],
        "analysis_summary": {"routing_class": "general"},
    })
    return behaviour


def _config(tmp_path):
    config = _deepcopy_default_config()
    config["auto_routing"]["provider_priority"] = ["serper", "brave", "exa"]
    config["v3"] = {
        "hedge_min_delay_seconds": 0.3,
        "attempt_timeout_seconds": 5,
        "state_path": str(tmp_path / "state.sqlite3"),
    }
    return config


def _execute(tmp_path, **request):
    req = search.legacy_request_to_v3("search", {"no_cache": True, "provider": "auto", **request})
    return execute_v3_request(req, search._search_adapter(), _config(tmp_path))


def _attempt(execution, provider):
    return next(a for a in execution.response.provider_attempts if a.provider == provider)


@pytest.mark.parametrize("exa_delay", [0.05, 1.5])
def test_research_member_preempted_after_quorum_keeps_status_ok(providers_at_http, tmp_path, exa_delay):
    providers_at_http[HOST["serper"]] = _answer("serper", 5, "s")
    providers_at_http[HOST["brave"]] = _answer("brave", 5, "b")
    providers_at_http[HOST["exa"]] = _answer("exa", 5, "e", delay=exa_delay)

    execution = _execute(tmp_path, query="research timing", mode="research", research_time_budget=8)
    response = execution.response.to_dict()

    # Serper and Brave form the quorum; Exa's answer is discarded by design.
    assert execution.legacy_payload["metadata"]["research_quorum"]["triggered"] is True
    assert execution.legacy_payload["metadata"]["providers_merged"] == ["serper", "brave"]
    assert len(response["results"]) == 5
    # Complete results, so no budget was hit: status is ok, whatever the timing.
    assert response["status"] == "ok"
    assert [w["code"] for w in response["warnings"]] == []
    assert response["routing_receipt"]["fallback_reason"] == "none"
    exa = _attempt(execution, "exa")
    assert exa.end_reason is AttemptEndReason.PREEMPTED_AFTER_QUORUM
    assert exa.outcome is AttemptOutcome.CANCELLED
    assert exa.error.error_class is ErrorClass.CANCELLED
    assert "timed out" not in exa.error.message
    assert exa.result_count == 0
    # The legacy diagnostic string is unchanged for Hermes users and tests.
    assert {"provider": "exa", "error": "preempted_after_quorum"} in execution.legacy_payload["routing"]["provider_errors"]
    # The wire form stays within the frozen schema and still says why it ended.
    wire = next(item for item in response["provider_attempts"] if item["provider"] == "exa")
    assert "end_reason" not in wire
    assert wire["error"]["details"]["end_reason"] == "preempted_after_quorum"


def test_research_member_over_the_time_budget_still_degrades(providers_at_http, tmp_path):
    providers_at_http[HOST["serper"]] = _answer("serper", 2, "s")
    providers_at_http[HOST["brave"]] = _answer("brave", 1, "s")  # same domains: no quorum
    providers_at_http[HOST["exa"]] = _answer("exa", 5, "e", delay=3.0)

    execution = _execute(tmp_path, query="research deadline", mode="research", research_time_budget=1)
    response = execution.response.to_dict()

    assert response["status"] == "degraded"
    assert "wsp.budget.limited" in [w["code"] for w in response["warnings"]]
    exa = _attempt(execution, "exa")
    assert exa.end_reason is AttemptEndReason.DEADLINE
    assert exa.outcome is AttemptOutcome.CANCELLED
    assert {"provider": "exa", "error": "timed out: research time budget exhausted"} in execution.legacy_payload["routing"]["provider_errors"]


def test_research_member_provider_error_is_a_provider_error(providers_at_http, tmp_path):
    # Exa fails at once; the quorum forms later, so its failure is on record.
    providers_at_http[HOST["serper"]] = _answer("serper", 5, "s", delay=0.3)
    providers_at_http[HOST["brave"]] = _answer("brave", 5, "b", delay=0.3)
    providers_at_http[HOST["exa"]] = _http_error(500)

    execution = _execute(tmp_path, query="research member down", mode="research", research_time_budget=8)
    response = execution.response.to_dict()

    assert response["status"] == "ok"
    assert len(response["results"]) == 5
    exa = _attempt(execution, "exa")
    assert exa.end_reason is AttemptEndReason.PROVIDER_ERROR
    assert exa.outcome is AttemptOutcome.FAILED
    # A member that really failed is still reported as a fallback situation.
    assert response["routing_receipt"]["fallback_reason"] == "selected_failed"


def test_skipped_primary_gives_one_fallback_reason_to_receipt_and_legacy_routing(providers_at_http, tmp_path):
    providers_at_http[HOST["serper"]] = _http_error(401)
    providers_at_http[HOST["brave"]] = _answer("brave", 2, "b")
    providers_at_http[HOST["exa"]] = _answer("exa", 2, "e")
    first = _execute(tmp_path, query="fallback probe")
    assert first.response.to_dict()["routing_receipt"]["fallback_reason"] == "selected_failed"
    assert first.legacy_payload["routing"]["fallback_reason"] == "selected_failed"

    # Serper is now blocked for auth and never called: skipped, not failed.
    second = _execute(tmp_path, query="fallback probe")
    receipt_reason = second.response.to_dict()["routing_receipt"]["fallback_reason"]
    assert receipt_reason == "selected_skipped"
    # Hermes (legacy payload) and MCP (v3 receipt) must name the same reason.
    assert second.legacy_payload["routing"]["fallback_reason"] == receipt_reason
    assert second.legacy_payload["routing"]["fallback_used"] is True
    assert second.legacy_payload["provider"] == "brave"
    serper = _attempt(second, "serper")
    assert serper.skip_reason is SkipReason.AUTH_BLOCKED
    assert serper.end_reason is AttemptEndReason.NOT_ADMITTED
    assert _attempt(first, "serper").end_reason is AttemptEndReason.PROVIDER_ERROR


def test_hedge_loser_is_superseded_not_timed_out(providers_at_http, tmp_path):
    providers_at_http[HOST["serper"]] = _answer("serper", 2, "s", delay=3.0)
    providers_at_http[HOST["brave"]] = _answer("brave", 2, "b")
    providers_at_http[HOST["exa"]] = _answer("exa", 2, "e")

    execution = _execute(tmp_path, query="hedge probe")
    response = execution.response.to_dict()

    assert response["status"] == "ok"
    assert [w["code"] for w in response["warnings"]] == []
    serper = _attempt(execution, "serper")
    assert serper.end_reason is AttemptEndReason.SUPERSEDED_BY_WINNER
    assert serper.outcome is AttemptOutcome.CANCELLED
    assert "timed out" not in serper.error.message
    assert response["routing_receipt"]["fallback_reason"] == "selected_failed"
    assert execution.legacy_payload["routing"]["fallback_reason"] == "selected_failed"


def test_attempt_end_reason_is_derived_for_old_cache_entries_and_kept_on_the_wire():
    old = {
        "attempt_id": "attempt_0123456789abcdef", "provider": "serper", "capability": "search",
        "outcome": "cancelled", "retry_count": 0, "result_count": 0, "started_at": "2026-11-08T00:00:00Z",
        "duration_ms": 1500, "circuit_state_before": "unknown", "circuit_state_after": "unknown",
        "endpoint_id": "serper:search", "decision": "attempted", "budget_decision": "unknown",
        "error": {"error_class": "timeout", "code": "wsp.provider.timeout", "message": "Provider request timed out", "retryable": True},
        "tries": [{"try_number": 1, "started_at": "2026-11-08T00:00:00Z", "duration_ms": 1500, "outcome": "error",
                   "error": {"error_class": "timeout", "code": "wsp.provider.timeout", "http_status": None, "retryable": True, "retry_after_ms": None}}],
    }
    # A v5.1.7 cache entry: cancelled without a stated reason was the deadline.
    attempt = ProviderAttemptV3.from_dict(old)
    assert attempt.end_reason is AttemptEndReason.DEADLINE
    assert attempt.to_dict() == {**old, "error": {**old["error"], "details": {}}}

    superseded = ProviderAttemptV3.from_dict({**old, "error": {**old["error"], "details": {"end_reason": "superseded_by_winner"}}})
    assert superseded.end_reason is AttemptEndReason.SUPERSEDED_BY_WINNER
    assert ProviderAttemptV3.from_dict(superseded.to_dict()).end_reason is AttemptEndReason.SUPERSEDED_BY_WINNER

    ok_try = [{"try_number": 1, "started_at": "2026-11-08T00:00:00Z", "duration_ms": 10, "outcome": "success", "error": None}]
    for payload, expected in [
        (dict(old, outcome="success", result_count=3, error=None, tries=ok_try), AttemptEndReason.ANSWERED),
        (dict(old, outcome="success", result_count=0, error=None, tries=ok_try), AttemptEndReason.ANSWERED_EMPTY),
        (dict(old, outcome="failed"), AttemptEndReason.PROVIDER_ERROR),
        (dict(old, outcome="skipped", decision="skipped", skip_reason="auth_blocked", error=None, tries=[]), AttemptEndReason.NOT_ADMITTED),
        (dict(old, outcome="skipped", decision="skipped", skip_reason="budget_blocked", error=None, tries=[]), AttemptEndReason.BUDGET_DENIED),
        (dict(old, outcome="skipped", decision="skipped", skip_reason="deadline_exceeded", error=None, tries=[]), AttemptEndReason.DEADLINE),
    ]:
        assert ProviderAttemptV3.from_dict({k: v for k, v in payload.items() if v is not None}).end_reason is expected, payload["outcome"]


def test_end_reason_must_agree_with_outcome():
    error = ErrorV3(error_class=ErrorClass.TRANSIENT, code="wsp.provider.transient", message="503", retryable=True)
    with pytest.raises(ValueError):
        ProviderAttemptV3(
            attempt_id="a", provider="serper", capability=Capability.SEARCH, outcome=AttemptOutcome.FAILED,
            error=error, end_reason=AttemptEndReason.ANSWERED,
        )
    with pytest.raises(ValueError):
        ProviderAttemptV3(
            attempt_id="a", provider="serper", capability=Capability.SEARCH, outcome=AttemptOutcome.SUCCESS,
            result_count=2, end_reason=AttemptEndReason.SUPERSEDED_BY_WINNER,
        )
