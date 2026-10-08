"""Regression coverage for the v3.1 self-hosted/no-paid-key profile."""

from __future__ import annotations

from copy import deepcopy
import json

import pytest

from wsp_core.compat_v3 import legacy_request_to_v3
from wsp_core import config
from wsp_core import extract
from wsp_core import search
from wsp_core.contract_v3 import Capability


from plugin_loader import load_plugin  # noqa: E402
wsp = load_plugin("wsp_plugin_test_v31_selfhosted_profile")
def _self_hosted_config() -> dict:
    value = deepcopy(config.DEFAULT_CONFIG)
    value["profile"] = "self_hosted"
    return config._validate_runtime_config(value)


def test_profile_validation_accepts_both_values_rejects_others_and_keeps_standard_auto_pool() -> None:
    before = deepcopy(config.DEFAULT_CONFIG["auto_routing"])
    standard = deepcopy(config.DEFAULT_CONFIG)
    standard["profile"] = "standard"

    assert config._validate_runtime_config(standard)["auto_routing"] == before
    assert _self_hosted_config()["profile"] == "self_hosted"

    invalid = deepcopy(config.DEFAULT_CONFIG)
    invalid["profile"] = "paid_only"
    with pytest.raises(ValueError, match="profile must be standard or self_hosted"):
        config._validate_runtime_config(invalid)


def test_searxng_base_url_is_canonical_with_legacy_instance_url_compatibility(monkeypatch) -> None:
    monkeypatch.setattr(config, "_validate_searxng_url", lambda value: value)
    runtime_config = deepcopy(config.DEFAULT_CONFIG)
    runtime_config["searxng"] = {
        "base_url": "https://base.example.test",
        "instance_url": "https://legacy.example.test",
    }

    assert config.get_searxng_instance_url(runtime_config) == "https://base.example.test"
    assert search.build_parser(runtime_config).parse_args([]).searxng_url == "https://base.example.test"


def test_self_hosted_derives_restricted_pools_and_explicit_keyed_search_warns(monkeypatch, tmp_path) -> None:
    runtime_config = _self_hosted_config()
    runtime_config["serper"] = {"api_key": "serper-test-key"}
    runtime_config["v3"] = {"state_path": str(tmp_path / "state.sqlite3")}

    auto = runtime_config["auto_routing"]
    assert auto["provider_priority"] == ["searxng", "keenable"]
    assert auto["fallback_provider"] == "keenable"
    assert auto["extract_provider_priority"] == ["keenable"]
    assert auto["auto_allow"]["serper"] is False
    assert auto["auto_allow"]["searxng"] is True
    assert auto["auto_allow"]["keenable"] is True
    assert extract.resolve_extract_provider_priority(runtime_config) == ["keenable"]

    monkeypatch.setattr(
        search,
        "search_serper",
        lambda **_kwargs: {
            "provider": "serper",
            "query": "q",
            "results": [{"url": "https://example.test", "title": "Result", "snippet": "snippet"}],
            "images": [],
            "answer": "",
            "metadata": {},
        },
    )
    result = search.run_search_request(
        query="q", provider="serper", config=runtime_config
    )

    assert result["provider"] == "serper"
    assert result["metadata"]["profile_deviation"] is True


def test_self_hosted_auto_without_searxng_or_keenable_returns_typed_error(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("SEARXNG_INSTANCE_URL", raising=False)
    monkeypatch.delenv("KEENABLE_API_KEY", raising=False)
    monkeypatch.delenv("KEENABLE_ALLOW_PUBLIC", raising=False)
    runtime_config = _self_hosted_config()
    runtime_config["v3"] = {"state_path": str(tmp_path / "state.sqlite3")}

    result = search.run_search_request(query="q", provider="auto", config=runtime_config)

    assert result["error_type"] == "self_hosted_profile_unavailable"
    assert "searxng.base_url" in result["error"]

    native = search.run_search_request_v3(
        legacy_request_to_v3(Capability.SEARCH, {"query": "q", "provider": "auto"}),
        config=runtime_config,
    )
    assert native.error.code == "wsp.config.self_hosted_profile_unavailable"


def test_status_displays_profile_pool_and_offline_prerequisites(tmp_path, capsys) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "profile": "self_hosted",
                "searxng": {"base_url": "https://search.example.test"},
            }
        )
    )
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(["status", "--plain", "--config-path", str(config_path)])

    args.func(args)

    output = capsys.readouterr().out
    assert "profile: self_hosted" in output
    assert "effective auto pool: searxng, keenable" in output
    assert "Profile diagnostics (offline)" in output
    assert "searxng.base_url" in output
