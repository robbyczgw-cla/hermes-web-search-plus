import gzip
import json
import unittest
from unittest import mock
import zlib

import search


class FakeResponse:
    def __init__(self, payload, headers=None):
        self.payload = payload
        self.headers = headers or {}

    def read(self):
        return self.payload

    def getheader(self, name):
        return self.headers.get(name)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class HttpContentEncodingTests(unittest.TestCase):
    def test_read_json_response_decompresses_gzip_header(self):
        body = {"web": {"results": []}}
        compressed = gzip.compress(json.dumps(body).encode("utf-8"))

        result = search._read_json_response(
            FakeResponse(compressed, {"Content-Encoding": "gzip"})
        )

        self.assertEqual(result, body)

    def test_read_json_response_decompresses_gzip_magic_without_header(self):
        body = {"ok": True}
        compressed = gzip.compress(json.dumps(body).encode("utf-8"))

        result = search._read_json_response(FakeResponse(compressed, {}))

        self.assertEqual(result, body)

    def test_read_json_response_decompresses_deflate_header(self):
        body = {"ok": "deflate"}
        compressed = zlib.compress(json.dumps(body).encode("utf-8"))

        result = search._read_json_response(
            FakeResponse(compressed, {"Content-Encoding": "deflate"})
        )

        self.assertEqual(result, body)

    def test_make_get_request_handles_gzip_urllib_response(self):
        body = {"web": {"results": [{"title": "Brave works"}]}}
        compressed = gzip.compress(json.dumps(body).encode("utf-8"))

        with mock.patch(
            "http_client.urlopen",
            return_value=FakeResponse(compressed, {"Content-Encoding": "gzip"}),
        ):
            result = search.make_get_request(
                "https://api.search.brave.com/res/v1/web/search?q=test",
                {"Accept": "application/json", "X-Subscription-Token": "test"},
            )

        self.assertEqual(result, body)

    def test_make_request_handles_gzip_urllib_response(self):
        body = {"organic": [{"title": "POST works"}]}
        compressed = gzip.compress(json.dumps(body).encode("utf-8"))

        with mock.patch(
            "http_client.urlopen",
            return_value=FakeResponse(compressed, {"Content-Encoding": "gzip"}),
        ):
            result = search.make_request(
                "https://example.test/search",
                {"Accept": "application/json"},
                {"q": "test"},
            )

        self.assertEqual(result, body)


if __name__ == "__main__":
    unittest.main()
