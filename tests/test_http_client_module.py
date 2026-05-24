import gzip
import json
from unittest import mock

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
