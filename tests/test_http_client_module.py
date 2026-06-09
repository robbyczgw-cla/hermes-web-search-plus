import gzip
import json
import time
from email.message import Message
from email.utils import formatdate
from io import BytesIO
from unittest import mock
from urllib.error import HTTPError

import pytest

import http_client


class FakeResponse:
    def __init__(self, body: bytes, headers=None):
        self._body = body
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_http_client_make_get_request_handles_gzip_urllib_response():
    body = {"web": {"results": [{"title": "Brave works"}]}}
    compressed = gzip.compress(json.dumps(body).encode("utf-8"))

    with mock.patch(
        "http_client.urlopen",
        return_value=FakeResponse(compressed, {"Content-Encoding": "gzip"}),
    ):
        result = http_client.make_get_request(
            "https://api.search.brave.com/res/v1/web/search?q=test",
            {"Accept": "application/json", "X-Subscription-Token": "test"},
        )

    assert result == body


def test_http_client_provider_request_error_exports_retry_metadata():
    error = http_client.ProviderRequestError("down", status_code=503, transient=True)

    assert str(error) == "down"
    assert error.status_code == 503
    assert error.transient is True


def test_default_user_agent_uses_release_version():
    assert http_client.DEFAULT_USER_AGENT == "ClawdBot-WebSearchPlus/2.4.0"


def _make_http_error(code: int, headers=None, body: bytes = b"{}") -> HTTPError:
    msg = Message()
    for name, value in (headers or {}).items():
        msg[name] = value
    return HTTPError("https://api.example.com/search", code, "error", msg, BytesIO(body))


def test_parse_retry_after_supports_delta_seconds():
    error = _make_http_error(429, {"Retry-After": "12"})
    assert http_client._parse_retry_after(error) == 12.0


def test_parse_retry_after_supports_http_date():
    error = _make_http_error(429, {"Retry-After": formatdate(time.time() + 60, usegmt=True)})
    parsed = http_client._parse_retry_after(error)
    assert parsed is not None
    assert 50 <= parsed <= 61


def test_parse_retry_after_handles_missing_and_garbage_values():
    assert http_client._parse_retry_after(_make_http_error(429)) is None
    assert http_client._parse_retry_after(_make_http_error(429, {"Retry-After": "soonish"})) is None


def test_rate_limit_error_carries_retry_after_metadata():
    with mock.patch("http_client.urlopen", side_effect=_make_http_error(429, {"Retry-After": "7"})):
        with pytest.raises(http_client.ProviderRequestError) as exc_info:
            http_client.make_request("https://api.example.com/search", {}, {"q": "test"})

    error = exc_info.value
    assert error.status_code == 429
    assert error.transient is True
    assert error.retry_after == 7.0


def test_service_unavailable_error_has_no_retry_after():
    with mock.patch("http_client.urlopen", side_effect=_make_http_error(503, {"Retry-After": "7"})):
        with pytest.raises(http_client.ProviderRequestError) as exc_info:
            http_client.make_request("https://api.example.com/search", {}, {"q": "test"})

    error = exc_info.value
    assert error.status_code == 503
    assert error.transient is True
    assert error.retry_after is None
