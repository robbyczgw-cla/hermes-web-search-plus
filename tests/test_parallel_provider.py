import importlib.util
import os
from pathlib import Path
from unittest import mock

SEARCH_PATH = Path(__file__).resolve().parents[1] / "search.py"
search_spec = importlib.util.spec_from_file_location("wsp_search_parallel_under_test", SEARCH_PATH)
search = importlib.util.module_from_spec(search_spec)
assert search_spec.loader is not None
search_spec.loader.exec_module(search)


def test_get_api_key_reads_parallel_env():
    with mock.patch.dict(os.environ, {"PARALLEL_API_KEY": "parallel-test-key"}, clear=False):
        assert search.get_api_key("parallel", {}) == "parallel-test-key"


def test_search_parallel_normalizes_excerpts_and_request_shape():
    fake_response = {
        "search_id": "search_123",
        "session_id": "sess_123",
        "results": [
            {
                "title": "Parallel docs",
                "url": "https://docs.parallel.ai/search",
                "excerpts": [{"text": "Search API excerpt."}, {"text": "Second excerpt."}],
            },
            {
                "url": "https://docs.parallel.ai/extract",
                "excerpts": ["Extract API excerpt."],
            },
        ],
    }
    with mock.patch.object(search, "make_request", return_value=fake_response) as mock_request:
        result = search.search_parallel(
            "Parallel AI Search API",
            "parallel-test-key",
            max_results=1,
            include_domains=["docs.parallel.ai"],
            exclude_domains=["example.com"],
        )

    assert result["provider"] == "parallel"
    assert result["metadata"]["search_id"] == "search_123"
    assert len(result["results"]) == 1
    assert result["results"][0]["snippet"] == "Search API excerpt.\n\nSecond excerpt."

    url, headers, body = mock_request.call_args.args[:3]
    assert url == "https://api.parallel.ai/v1/search"
    assert headers["x-api-key"] == "parallel-test-key"
    assert body["objective"] == "Parallel AI Search API"
    assert body["search_queries"] == ["Parallel AI Search API site:docs.parallel.ai -site:example.com"]
    assert "max_results" not in body


def test_extract_parallel_requests_full_content_and_normalizes_results():
    fake_response = {
        "search_id": "extract_123",
        "results": [
            {
                "url": "https://docs.parallel.ai/getting-started/overview",
                "title": "Overview",
                "full_content": "Full extracted markdown",
                "excerpts": [{"text": "Short excerpt"}],
            }
        ],
    }
    with mock.patch.object(search, "make_request", return_value=fake_response) as mock_request:
        result = search.extract_parallel(
            ["https://docs.parallel.ai/getting-started/overview"],
            "parallel-test-key",
            max_chars_total=1234,
            max_chars_per_result=567,
        )

    assert result["provider"] == "parallel"
    assert result["metadata"]["search_id"] == "extract_123"
    item = result["results"][0]
    assert item["provider"] == "parallel"
    assert item["title"] == "Overview"
    assert item["content"] == "Full extracted markdown"
    assert item["raw_content"] == "Full extracted markdown"

    url, headers, body = mock_request.call_args.args[:3]
    assert url == "https://api.parallel.ai/v1/extract"
    assert headers["x-api-key"] == "parallel-test-key"
    assert body["urls"] == ["https://docs.parallel.ai/getting-started/overview"]
    assert body["max_chars_total"] == 1234
    assert body["advanced_settings"]["full_content"]["max_chars_per_result"] == 567


def test_parallel_is_explicit_provider_but_blocked_from_auto_by_default():
    config = search._deepcopy_default_config()
    assert "parallel" in config["auto_routing"]["provider_priority"]
    assert config["auto_routing"]["auto_allow"]["parallel"] is False

    with mock.patch.dict(os.environ, {"PARALLEL_API_KEY": "parallel-test-key"}, clear=False):
        assert search.validate_api_key("parallel", config) == "parallel-test-key"


def test_validate_api_key_parallel_missing_key_raises_provider_config_error():
    config = search._deepcopy_default_config()
    config["parallel"].pop("api_key", None)
    with mock.patch.dict(os.environ, {}, clear=True):
        try:
            search.validate_api_key("parallel", config)
        except search.ProviderConfigError as exc:
            assert "PARALLEL_API_KEY" in str(exc)
            assert "platform.parallel.ai" in str(exc)
        else:
            raise AssertionError("validate_api_key should fail cleanly when PARALLEL_API_KEY is missing")


def test_existing_priority_config_appends_parallel_for_migration():
    config = search._deepcopy_default_config()
    config["auto_routing"]["provider_priority"] = ["tavily", "linkup", "serper"]

    migrated = search._validate_runtime_config(config)

    priority = migrated["auto_routing"]["provider_priority"]
    assert priority[:3] == ["tavily", "linkup", "serper"]
    assert "parallel" in priority
