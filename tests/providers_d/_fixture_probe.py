"""Subprocess probe: assert SDK fixture-provider discovery end to end.

Runs with WSP_SDK_ALLOW_NON_PRODUCTION=1 so the non-production example
fixture is discovered without ever widening the default provider surface
of the importing test process.
"""
from __future__ import annotations

from plugin_loader import load_plugin
from wsp_core import bench
from wsp_core import provider_registry
from wsp_core import routing
from wsp_core import search


def main() -> None:
    plugin = load_plugin("wsp_fixture_probe_plugin")
    config = {
        "example_fixture": {"allow_public": True},
        "auto_routing": {"provider_priority": ["example-fixture"]},
    }

    spec = provider_registry.PROVIDER_SPECS["example-fixture"]
    assert spec.production is False
    assert spec.execute_search is not None
    assert "example-fixture" in provider_registry.SEARCH_PROVIDER_IDS
    assert "example-fixture" not in provider_registry.DEFAULT_PROVIDER_PRIORITY
    assert provider_registry.DEFAULT_AUTO_ALLOW["example-fixture"] is False
    assert routing._provider_auto_allowed("example-fixture", {}) is False

    result = search.run_search_request(
        query="SDK fixture", provider="example-fixture", config=config
    )
    assert result["provider"] == "example-fixture"
    assert result["results"][0]["url"] == "https://example.invalid/wsp-sdk-fixture"

    parser = search.build_parser(config)
    provider_action = next(
        action for action in parser._actions if "--provider" in action.option_strings
    )
    assert "example-fixture" in provider_action.choices

    status = plugin._provider_config_status(env={})["providers"]
    assert status["example-fixture"]["display_name"] == "Example fixture (non-production)"

    report = search._build_doctor_report(config)
    doctor = {item["provider"]: item for item in report["providers"]}
    assert doctor["example-fixture"]["search_capable"] is True

    bench_report = bench.run_bench(
        config,
        providers=["example-fixture"],
        queries=[{"id": "fixture", "query": "SDK fixture"}],
    )
    assert bench_report["providers"][0]["provider"] == "example-fixture"
    assert bench_report["providers"][0]["success_count"] == 1
    print("FIXTURE_PROBE_OK")


if __name__ == "__main__":
    main()
