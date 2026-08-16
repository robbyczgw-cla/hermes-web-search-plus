from __future__ import annotations

import time

import search
from research import run_research_mode


def _routing() -> dict:
    return {
        "provider": "you",
        "confidence": 0.9,
        "confidence_level": "high",
        "reason": "fixture",
        "routing_policy": "routing-v2",
        "top_signals": [],
        "scores": {"you": 1.0},
        "auto_allow_excluded": ["donsetch", "serpbase"],
        "analysis_summary": {"routing_class": "research"},
    }


def _config(tmp_path) -> dict:
    config = search._deepcopy_default_config()
    config["auto_routing"]["provider_priority"] = ["you"]
    config["auto_routing"]["disabled_providers"] = []
    config["auto_routing"]["auto_allow"].update(
        {"donsetch": False, "serpbase": False, "you": True}
    )
    config["quality"]["research_quorum"]["enabled"] = False
    v3 = config.setdefault("v3", {})
    v3["state_path"] = str(tmp_path / "state.sqlite3")
    v3["cache_dir"] = str(tmp_path)
    return config


def _provider_payload(provider: str) -> dict:
    return {
        "provider": provider,
        "query": "fixture",
        "results": [
            {
                "title": provider,
                "url": f"https://{provider}.example/source",
                "snippet": "evidence",
            }
        ],
    }


def _install_fakes(monkeypatch, calls: list[str], cooldown_provider: str | None = None):
    monkeypatch.setattr(search, "auto_route_provider", lambda *_args: _routing())
    monkeypatch.setattr(search, "provider_configured", lambda *_args: True)
    monkeypatch.setattr(search, "validate_api_key", lambda *_args: "fixture-key")
    monkeypatch.setattr(
        search,
        "provider_in_cooldown",
        lambda provider: (provider == cooldown_provider, 42 if provider == cooldown_provider else 0),
    )
    monkeypatch.setattr(search, "record_provider_outcome", lambda *_args, **_kwargs: None)

    def fake_adapter(provider: str):
        def execute(_namespace, _provider, _args, _key, _config, _routing):
            calls.append(provider)
            return _provider_payload(provider)

        return execute

    for provider in ("donsetch", "serpbase", "you"):
        monkeypatch.setitem(
            search.SEARCH_DISPATCH, provider, fake_adapter(provider)
        )


def _args(config: dict, providers: list[str]):
    return search.build_parser(config).parse_args(
        [
            "--query",
            "explicit research fixture",
            "--mode",
            "research",
            "--research-providers",
            *providers,
            "--research-extract-count",
            "0",
            "--research-time-budget",
            "5",
            "--no-cache",
        ]
    )


def test_explicit_research_providers_bypass_auto_allow(tmp_path, monkeypatch):
    calls: list[str] = []
    config = _config(tmp_path)
    _install_fakes(monkeypatch, calls)

    result, status = search._execute_search_request_core(
        _args(config, ["donsetch", "serpbase", "you"]), config
    )

    assert status == 0
    assert set(calls) == {"donsetch", "serpbase", "you"}
    assert result["routing"]["providers_queried"] == ["donsetch", "serpbase", "you"]
    assert result["quality_report"]["eligible_providers"] == [
        "donsetch",
        "serpbase",
        "you",
    ]


def test_explicit_research_cooldown_skip_is_visible(tmp_path, monkeypatch):
    calls: list[str] = []
    config = _config(tmp_path)
    _install_fakes(monkeypatch, calls, cooldown_provider="donsetch")

    result, status = search._execute_search_request_core(
        _args(config, ["donsetch", "you"]), config
    )

    assert status == 0
    assert calls == ["you"]
    assert result["routing"]["providers_skipped"] == [
        {
            "provider": "donsetch",
            "reason": "cooldown",
            "cooldown_remaining_seconds": 42,
        }
    ]
    assert result["quality_report"]["skipped_providers"] == [
        {
            "provider": "donsetch",
            "reason": "cooldown",
            "cooldown_remaining_seconds": 42,
        }
    ]


def test_quorum_counts_later_provider_when_first_fills_result_target():
    def execute(provider: str) -> dict:
        if provider == "slow":
            time.sleep(1.0)
        if provider == "first":
            urls = [f"https://first-{index}.example/source" for index in range(5)]
        elif provider == "second":
            urls = ["https://second.example/source"]
        else:
            urls = ["https://slow.example/source"]
        return {
            "provider": provider,
            "results": [
                {"title": provider, "url": url, "snippet": "evidence"}
                for url in urls
            ],
        }

    started = time.monotonic()
    result = run_research_mode(
        query="saturated first provider",
        research_providers=["first", "second", "slow"],
        execute_search=execute,
        extract_urls=lambda _urls: {"provider": None, "results": []},
        max_results=5,
        max_extract_urls=0,
        time_budget_seconds=3.0,
    )

    assert time.monotonic() - started < 0.5
    quorum = result["metadata"]["research_quorum"]
    assert quorum["triggered"] is True
    assert quorum["contributing_providers"] == ["first", "second"]
    assert quorum["deduplicated_result_count"] == 6
    assert result["routing"]["provider_errors"] == [
        {"provider": "slow", "error": "preempted_after_quorum"}
    ]
