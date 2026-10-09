"""SDK extraction providers must be cacheable (finding: identity raised for them)."""

from __future__ import annotations

import pytest

from wsp_core.extract import _extract_provider_endpoint_config
from wsp_core.provider_registry import PROVIDER_SPECS
from wsp_sdk import ProviderSpec


def _sdk_extract_spec() -> ProviderSpec:
    def execute_extract(url, key, config, timeout):
        raise AssertionError("identity derivation must not dispatch")

    return ProviderSpec(
        id="acme-extract",
        kind="extract",
        env_var="ACME_EXTRACT_KEY",
        display_name="Acme Extract",
        description="SDK extraction fixture for cache identity",
        config_section="acme_extract",
        signup_url="https://example.invalid",
        execute_extract=execute_extract,
    )


@pytest.fixture()
def sdk_provider(monkeypatch):
    spec = _sdk_extract_spec()
    monkeypatch.setitem(PROVIDER_SPECS, spec.provider, spec)
    return spec


def test_sdk_extract_provider_gets_deterministic_identity(sdk_provider):
    config = {
        "acme_extract": {
            "endpoint": "https://api.acme.invalid/extract",
            "timeout": 45,
            "api_key": "sk-super-secret",
            "auth_token": "also-secret",
            "nested": {"ignored": True},
        }
    }

    identity = _extract_provider_endpoint_config("acme-extract", config)

    assert identity == {
        "sdk_provider": "acme-extract",
        "config_section": "acme_extract",
        "settings": {
            "endpoint": "https://api.acme.invalid/extract",
            "timeout": 45,
        },
    }
    assert "sk-super-secret" not in repr(identity)
    assert "also-secret" not in repr(identity)


def test_sdk_extract_identity_is_stable_without_config_section(sdk_provider):
    assert _extract_provider_endpoint_config("acme-extract", {}) == (
        _extract_provider_endpoint_config("acme-extract", {"acme_extract": {}})
    )


def test_unknown_provider_still_fails_closed():
    with pytest.raises(ValueError, match="unknown extraction provider"):
        _extract_provider_endpoint_config("never-registered", {})
