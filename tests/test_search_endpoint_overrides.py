"""Search endpoint overrides for Brave, Tavily and Exa.

These three providers used to hardcode their official search URLs, unlike
Firecrawl/Linkup/SerpBase whose ``api_url`` is operator-configurable. The
overrides let an operator point search at a compatible proxy (for example a
multi-key pool gateway) through config.json:

- ``brave.api_url``    -> full search endpoint URL
- ``tavily.search_url`` -> full search endpoint URL
- ``exa.base_url``     -> base URL; ``/search`` and ``/findSimilar`` are appended

Defaults are exactly the previous hardcoded official endpoints.
"""
from unittest import mock

from wsp_core import provider_dispatch, providers, search

POOL = "https://pool.example.com"


# ---------------------------------------------------------------------------
# Provider functions honour a custom endpoint
# ---------------------------------------------------------------------------

def test_search_brave_uses_custom_api_url():
    captured = {}

    def fake_get(url, headers):
        captured["url"] = url
        return {"web": {"results": []}}

    with mock.patch.object(providers, "_brave_get", fake_get):
        providers.search_brave(
            query="example",
            api_key="brave-key",
            api_url=f"{POOL}/brave/res/v1/web/search",
        )

    assert captured["url"].startswith(f"{POOL}/brave/res/v1/web/search?")


def test_search_tavily_uses_custom_api_url():
    fake_response = {"results": []}
    with mock.patch(
        "wsp_core.providers.make_request", return_value=fake_response
    ) as mock_request:
        providers.search_tavily(
            query="example",
            api_key="tavily-key",
            api_url=f"{POOL}/tavily/search",
        )

    assert mock_request.call_args.args[0] == f"{POOL}/tavily/search"


def test_search_exa_uses_custom_base_url_for_search_and_similar():
    fake_response = {"results": []}
    with mock.patch(
        "wsp_core.providers.make_request", return_value=fake_response
    ) as mock_request:
        providers.search_exa(
            query="example",
            api_key="exa-key",
            base_url=f"{POOL}/exa",
        )
        assert mock_request.call_args.args[0] == f"{POOL}/exa/search"

        providers.search_exa(
            query="",
            api_key="exa-key",
            similar_url="https://example.com/page",
            base_url=f"{POOL}/exa/",
        )
        assert mock_request.call_args.args[0] == f"{POOL}/exa/findSimilar"


# ---------------------------------------------------------------------------
# Dispatch forwards config values, and defaults match the official endpoints
# ---------------------------------------------------------------------------

def _dispatch_kwargs(provider, config):
    args = search.build_parser({}).parse_args(["-q", "q"])
    call = mock.Mock(return_value={"results": []})
    provider_dispatch.SEARCH_DISPATCH[provider](
        {"search_" + provider: call}, provider, args, "key", config, {}
    )
    return call.call_args.kwargs


def test_dispatch_brave_api_url_from_config_and_default():
    custom = _dispatch_kwargs("brave", {"brave": {"api_url": f"{POOL}/brave/res/v1/web/search"}})
    assert custom["api_url"] == f"{POOL}/brave/res/v1/web/search"

    default = _dispatch_kwargs("brave", {})
    assert default["api_url"] == "https://api.search.brave.com/res/v1/web/search"


def test_dispatch_tavily_search_url_from_config_and_default():
    custom = _dispatch_kwargs("tavily", {"tavily": {"search_url": f"{POOL}/tavily/search"}})
    assert custom["api_url"] == f"{POOL}/tavily/search"

    default = _dispatch_kwargs("tavily", {})
    assert default["api_url"] == "https://api.tavily.com/search"


def test_dispatch_exa_base_url_from_config_and_default():
    custom = _dispatch_kwargs("exa", {"exa": {"base_url": f"{POOL}/exa"}})
    assert custom["base_url"] == f"{POOL}/exa"

    default = _dispatch_kwargs("exa", {})
    assert default["base_url"] == "https://api.exa.ai"
