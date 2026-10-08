"""Missing-key guidance must survive the v3 error classification.

The v3 taxonomy deliberately never copies upstream exception text. WSP's own
missing-key guidance (raised by ``config.validate_api_key``) is the exception:
it is generated locally, matched against a strict shape, and surfaced through
``ErrorV3.details`` so agents and MCP clients can tell the user what to set.
"""

import json

import pytest

from config import ProviderConfigError, validate_api_key
from errors_v3 import classify_provider_error


def _config_error(provider):
    with pytest.raises(ProviderConfigError) as caught:
        validate_api_key(provider, {})
    return caught.value


def test_missing_key_guidance_survives_classification(monkeypatch):
    monkeypatch.delenv("TINYFISH_API_KEY", raising=False)
    error = classify_provider_error(_config_error("tinyfish"), provider="tinyfish")
    assert error.code == "wsp.config.provider_invalid"
    assert error.message == "Missing API key for tinyfish"
    assert error.details["env_var"] == "TINYFISH_API_KEY"
    assert error.details["setup_required"] is True
    assert error.details["how_to_fix"]


def test_missing_searxng_url_guidance_survives(monkeypatch):
    monkeypatch.delenv("SEARXNG_INSTANCE_URL", raising=False)
    error = classify_provider_error(_config_error("searxng"), provider="searxng")
    assert error.message == "Missing SearXNG instance URL"
    assert error.details["env_var"] == "SEARXNG_INSTANCE_URL"


@pytest.mark.parametrize(
    "body",
    [
        "upstream said: token sk-live-123 is wrong",
        json.dumps({"error": "SearXNG instance URL must start with http:// or https://", "provided": "ftp://secret.internal"}),
        json.dumps({"error": "Missing API key for tinyfish", "env_var": "x; rm -rf /", "how_to_fix": ["a"]}),
        json.dumps({"error": "Missing API key for tinyfish https://evil.example/?k=1", "env_var": "TINYFISH_API_KEY", "how_to_fix": ["a"]}),
        json.dumps(["not", "a", "dict"]),
    ],
)
def test_untrusted_config_error_text_stays_redacted(body):
    error = classify_provider_error(ProviderConfigError(body), provider="tinyfish")
    assert error.message == "Provider configuration is invalid"
    assert error.details == {}
    dumped = json.dumps(error.to_dict())
    assert "secret.internal" not in dumped
    assert "sk-live" not in dumped
