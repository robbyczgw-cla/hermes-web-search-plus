import json
from unittest import mock

import pytest

from wsp_core import providers


@pytest.fixture
def fake_response():
    return {
        "markdown": "Extracted **content**",
        "rawHtml": "<p>Extracted content</p>",
        "metadata": {"title": "Test Page"},
    }


def test_extract_fmf_tools_calls_make_request(fake_response):
    with mock.patch.object(providers, "make_request", return_value=fake_response) as mock_req:
        result = providers.extract_fmf_tools(url="https://example.com")
        mock_req.assert_called_once()
        called_url = mock_req.call_args[0][0]
        assert called_url == "https://mpp-agent-data.fmf-x402.workers.dev/mpp/web/scrape"
        assert result["markdown"] == fake_response["markdown"]
        assert result["raw_html"] == fake_response["rawHtml"]
        assert result["metadata"] == fake_response["metadata"]
