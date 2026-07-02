import gzip
import json
import socket
import time
from email.message import Message
from email.utils import formatdate
from io import BytesIO
from unittest import mock
from urllib.error import HTTPError, URLError

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
    assert http_client.DEFAULT_USER_AGENT == "ClawdBot-WebSearchPlus/2.8.1"


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


def test_socket_timeout_is_classified_transient():
    # socket.timeout is only a TimeoutError alias on Python 3.10+; a raw
    # read timeout on 3.8/3.9 must still map to a transient provider error.
    for func in (
        lambda: http_client.make_request("https://api.example.com/search", {}, {"q": "test"}),
        lambda: http_client.make_get_request("https://api.example.com/search", {}),
    ):
        with mock.patch("http_client.urlopen", side_effect=socket.timeout("timed out")):
            with pytest.raises(http_client.ProviderRequestError) as exc_info:
                func()
        assert exc_info.value.transient is True


def test_urlerror_wrapping_socket_timeout_is_transient():
    # The reason's str() ("_") does not contain "timed out"; classification
    # must rely on the exception type, not the message text.
    with mock.patch("http_client.urlopen", side_effect=URLError(socket.timeout("_"))):
        with pytest.raises(http_client.ProviderRequestError) as exc_info:
            http_client.make_request("https://api.example.com/search", {}, {"q": "test"})
    assert exc_info.value.transient is True


def test_invalid_json_response_raises_provider_error():
    with mock.patch("http_client.urlopen", return_value=FakeResponse(b"<html>bad gateway</html>")):
        with pytest.raises(http_client.ProviderRequestError) as exc_info:
            http_client.make_get_request("https://api.example.com/search", {})
    assert exc_info.value.transient is True


def test_non_utf8_response_raises_provider_error():
    with mock.patch("http_client.urlopen", return_value=FakeResponse(b"\xff\xfe\xfa")):
        with pytest.raises(http_client.ProviderRequestError) as exc_info:
            http_client.make_get_request("https://api.example.com/search", {})
    assert exc_info.value.transient is True


def test_corrupt_gzip_response_raises_provider_error():
    response = FakeResponse(b"\x1f\x8bgarbage", {"Content-Encoding": "gzip"})
    with mock.patch("http_client.urlopen", return_value=response):
        with pytest.raises(http_client.ProviderRequestError) as exc_info:
            http_client.make_get_request("https://api.example.com/search", {})
    assert exc_info.value.transient is True


def test_corrupt_deflate_response_raises_provider_error():
    response = FakeResponse(b"not-deflate", {"Content-Encoding": "deflate"})
    with mock.patch("http_client.urlopen", return_value=response):
        with pytest.raises(http_client.ProviderRequestError) as exc_info:
            http_client.make_get_request("https://api.example.com/search", {})
    assert exc_info.value.transient is True


def test_http_error_with_corrupt_body_still_reports_status():
    error = _make_http_error(500, {"Content-Encoding": "gzip"}, body=b"\x1f\x8bgarbage")
    with mock.patch("http_client.urlopen", side_effect=error):
        with pytest.raises(http_client.ProviderRequestError) as exc_info:
            http_client.make_request("https://api.example.com/search", {}, {"q": "test"})
    assert exc_info.value.status_code == 500


def test_http_error_with_non_utf8_body_does_not_crash_detail_extraction():
    error = _make_http_error(500, body=b"\xff\xfe server exploded")
    with mock.patch("http_client.urlopen", side_effect=error):
        with pytest.raises(http_client.ProviderRequestError) as exc_info:
            http_client.make_request("https://api.example.com/search", {}, {"q": "test"})
    assert exc_info.value.status_code == 500
