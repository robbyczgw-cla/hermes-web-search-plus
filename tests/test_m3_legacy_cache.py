from __future__ import annotations

import cache
import search
from compat_v3 import legacy_request_to_v3
from contract_v3 import Capability


def test_v2_search_cache_is_read_only_legacy_hit_and_promotes_to_v3(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(search, "CACHE_DIR", tmp_path)
    config = {
        "version": 1,
        "auto_routing": {
            "enabled": True,
            "provider_priority": ["serper"],
            "disabled_providers": [],
        },
        "v3": {
            "cache_dir": str(tmp_path),
            "state_path": str(tmp_path / "v3" / "state.sqlite3"),
        },
    }
    request = legacy_request_to_v3(
        Capability.SEARCH,
        {"query": "legacy cache", "provider": "serper", "count": 2},
        request_id="current-id",
    )
    request = type(request).from_dict(
        {**request.to_dict(), "cache": {**request.cache, "mode": "only"}}
    )
    args = search._search_args_from_v3(request, config)
    args.provider = "serper"
    params = search._legacy_search_cache_context(args, "serper", config)
    legacy_payload = {
        "provider": "serper",
        "query": "legacy cache",
        "results": [
            {
                "title": "Cached",
                "url": "https://example.com/cached",
                "snippet": "from v2",
            }
        ],
    }
    cache.cache_put(
        "legacy cache", "serper", 2, legacy_payload, params=params
    )
    v2_path = cache._get_cache_path(
        cache._get_cache_key("legacy cache", "serper", 2, params)
    )
    before = v2_path.read_bytes()

    monkeypatch.setattr(
        search,
        "_execute_search_request_core",
        lambda *_: (_ for _ in ()).throw(AssertionError("provider called")),
    )
    execution = search.execute_v3_request(request, search._search_adapter(), config)

    assert execution.response.cache_status["disposition"] == "fresh_hit"
    assert execution.response.cache_status["source_contract_version"] == "2.x"
    assert execution.response.provider_attempts == []
    assert execution.legacy_payload["results"] == legacy_payload["results"]
    assert v2_path.read_bytes() == before
    assert list((tmp_path / "v3" / "response" / "search").glob("*.json"))
