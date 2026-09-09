"""Wire-to-v3 regressions: never derive receipt evidence from a second clock."""

import json
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

import provider_dispatch
import providers
import search


@pytest.mark.parametrize(
    "provider",
    [
        "serper",
        "brave",
        "tavily",
        "querit",
        "exa",
        "firecrawl",
        "you",
        "searxng",
        "keenable",
    ],
)
def test_dispatch_accepts_legacy_namespace_without_time_range(provider, monkeypatch):
    monkeypatch.setattr(provider_dispatch, "_validate_searxng_url", lambda url: url)
    args = search.build_parser({}).parse_args(["-q", "q", "--freshness", "week"])
    del args.time_range
    call = mock.Mock(return_value={"results": []})
    provider_dispatch.SEARCH_DISPATCH[provider](
        {"search_" + provider: call}, provider, args, "https://example.test", {}, {}
    )
    kwargs = call.call_args.kwargs
    assert kwargs.get("time_range", kwargs.get("freshness")) == "week"


@pytest.mark.parametrize(
    "recency,freshness",
    [("week", None), ("hour", None), ("hour", "week"), ("unknown", "week")],
)
def test_exa_wire_survives_run_search_request_and_v3_serialization(
    monkeypatch, tmp_path, recency, freshness
):
    clock = mock.Mock(
        side_effect=[
            datetime(2026, 9, 9, 12, tzinfo=timezone.utc),
            datetime(2099, 1, 1, tzinfo=timezone.utc),
        ]
    )
    real_bounds = providers.exa_date_bounds
    bounds = mock.Mock(side_effect=lambda token: real_bounds(token, now=clock()))
    monkeypatch.setattr(providers, "exa_date_bounds", bounds)
    bodies = []

    def http(_url, _headers, body, **_kwargs):
        bodies.append(dict(body))
        return {
            "results": [
                {
                    "url": "https://example.test/a",
                    "title": "A",
                    "text": "boilerplate",
                    "highlights": ["Relevant evidence"],
                }
            ]
        }

    monkeypatch.setattr(providers, "make_request", http)
    monkeypatch.setattr(search, "make_request", http)
    executions = []
    execute = search.execute_v3_request

    def capture(*args, **kwargs):
        execution = execute(*args, **kwargs)
        executions.append(execution)
        return execution

    monkeypatch.setattr(search, "execute_v3_request", capture)
    result = search.run_search_request(
        query="recency wire evidence",
        provider="exa",
        time_range=recency,
        freshness=freshness,
        config={
            "exa": {"api_key": "exa-test-key"},
            "v3": {"state_path": str(tmp_path / "state.sqlite3")},
            "auto_routing": {"provider_priority": ["exa"], "disabled_providers": []},
        },
    )
    assert not result.get("error"), result
    assert len(bodies) == 1
    sent = {
        k: v
        for k, v in bodies[0].items()
        if k in {"startPublishedDate", "endPublishedDate"}
    }
    serialized = json.loads(json.dumps(executions[0].response.to_dict()))
    assert "metadata" not in serialized
    warning = next(
        w for w in serialized["warnings"] if w["code"] == "wsp.freshness.applied"
    )
    meta = warning["details"]["freshness"]
    assert meta == result["metadata"]["freshness"]
    assert meta["requested"] == recency
    assert meta["applied"] is bool(sent)
    assert meta.get("native_value", {}) == sent
    assert result["results"][0]["snippet"] == "Relevant evidence"
    if sent:
        expected = timedelta(hours=1) if recency == "hour" else timedelta(days=7)
        assert (
            datetime.fromisoformat(sent["endPublishedDate"])
            - datetime.fromisoformat(sent["startPublishedDate"])
            == expected
        )
    bounds.assert_called_once_with(recency)
    clock.assert_called_once()


@pytest.mark.parametrize(
    "time_range,freshness,expected",
    [(None, "week", "week"), ("day", None, "day"), ("day", "week", "day")],
)
def test_tavily_metadata_matches_http(monkeypatch, time_range, freshness, expected):
    bodies = []

    def http(_url, _headers, body, **_kwargs):
        bodies.append(body)
        return {
            "results": [
                {"url": "https://example.test/a", "title": "A", "content": "Evidence"}
            ]
        }

    monkeypatch.setattr(providers, "make_request", http)
    monkeypatch.setattr(search, "make_request", http)
    result = search.run_search_request(
        query="tavily wire recency",
        provider="tavily",
        time_range=time_range,
        freshness=freshness,
        config={
            "tavily": {"api_key": "tvly-test-key"},
            "auto_routing": {"provider_priority": ["tavily"]},
        },
    )
    assert not result.get("error"), result
    assert len(bodies) == 1
    assert bodies[0]["time_range"] == expected
    assert result["metadata"]["freshness"] == {
        "requested": expected,
        "applied": True,
        "provider": "tavily",
        "native_value": expected,
    }


def test_pipeline_accepts_legacy_namespace_without_time_range(monkeypatch):
    config = {
        "tavily": {"api_key": "tvly-test-key"},
        "auto_routing": {"provider_priority": ["tavily"]},
    }
    args = search.build_parser(config).parse_args(
        ["-q", "q", "-p", "tavily", "--freshness", "week", "--no-cache"]
    )
    del args.time_range
    args._v3_engine_owned_attempt = True
    http = mock.Mock(
        return_value={
            "results": [
                {"url": "https://example.test/a", "title": "A", "content": "Evidence"}
            ]
        }
    )
    monkeypatch.setattr(providers, "make_request", http)
    monkeypatch.setattr(search, "make_request", http)
    result, code = search._execute_search_request_core(args, config)
    assert code == 0, result
    assert http.call_args.args[2]["time_range"] == "week"
    assert result["metadata"]["freshness"]["native_value"] == "week"


def test_cli_accepts_hour_conflict():
    args = search.build_parser({}).parse_args(
        ["-q", "q", "--time-range", "hour", "--freshness", "week"]
    )
    assert providers.effective_recency(args.time_range, args.freshness) == "hour"


def test_research_exa_receipt_uses_wire_dates(monkeypatch):
    sent = {
        "startPublishedDate": "2026-09-09T11:00:00Z",
        "endPublishedDate": "2026-09-09T12:00:00Z",
    }
    bodies = []

    def http(_url, _headers, body, **_kwargs):
        bodies.append(body)
        return {
            "results": [
                {"url": "https://example.test/a", "title": "A", "text": "Evidence"}
            ]
        }

    monkeypatch.setattr(providers, "make_request", http)
    monkeypatch.setattr(search, "make_request", http)
    monkeypatch.setattr(search, "extract_plus", lambda **kwargs: {"results": []})
    with mock.patch.object(
        providers,
        "exa_date_bounds",
        side_effect=[
            tuple(sent.values()),
            ("2099-01-01T00:00:00Z", "2099-01-01T01:00:00Z"),
        ],
    ) as bounds:
        result = search.run_search_request(
            query="research wire recency",
            provider="exa",
            mode="research",
            time_range="hour",
            freshness="week",
            config={
                "exa": {"api_key": "exa-test-key"},
                "auto_routing": {"provider_priority": ["exa"]},
            },
        )
    assert not result.get("error"), result
    assert len(bodies) == 1
    assert {key: bodies[0][key] for key in sent} == sent
    meta = next(
        item
        for item in result["metadata"]["freshness"]["providers"]
        if item["provider"] == "exa"
    )
    assert meta == {
        "requested": "hour",
        "applied": True,
        "provider": "exa",
        "native_value": sent,
    }
    bounds.assert_called_once_with("hour")


@pytest.mark.parametrize("sent", [{}, {"startPublishedDate": "2020-01-01T00:00:00Z"}])
def test_exa_metadata_uses_only_returned_dates(sent):
    with mock.patch.object(
        providers,
        "exa_date_bounds",
        side_effect=AssertionError("receipt must not consult clock"),
    ):
        meta = providers.freshness_metadata("exa", "week", applied_published_dates=sent)
    assert meta["applied"] is bool(sent)
    assert meta.get("native_value", {}) == sent
