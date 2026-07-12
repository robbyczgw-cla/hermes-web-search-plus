from __future__ import annotations

import json

from cache_v3 import ResponseCacheV3, derive_cache_key
from compat_v3 import legacy_request_to_v3
from contract_v3 import Capability, RequestV3


def _request(*, request_id: str, reordered: bool = False) -> RequestV3:
    request = legacy_request_to_v3(
        Capability.SEARCH,
        {
            "query": "Café models",
            "provider": "serper",
            "count": 3,
            "include_domains": ["example.com"],
        },
        request_id=request_id,
    )
    if not reordered:
        return request
    payload = request.to_dict()
    payload["options"] = dict(reversed(list(payload["options"].items())))
    payload["routing"] = dict(reversed(list(payload["routing"].items())))
    return RequestV3.from_dict(payload)


def _response_payload(request_id: str = "stored-id") -> dict:
    return {
        "contract_version": "3.0",
        "request_id": request_id,
        "execution_id": "exec_stored",
        "capability": "search",
        "status": "ok",
        "results": [],
        "provider_attempts": [],
        "routing_receipt": {
            "policy_id": "classic",
            "policy_revision": "v2.9.1",
            "mode": "classic",
            "candidate_order": ["serper"],
            "selected_provider": "serper",
            "fallback_reason": "none",
        },
        "cache_status": {"disposition": "miss"},
        "limits_applied": {"max_results": 3},
        "dedup_clusters": [],
        "warnings": [],
    }


def test_cache_key_is_deterministic_without_request_id_or_dict_order():
    first = derive_cache_key(_request(request_id="one"))
    second = derive_cache_key(_request(request_id="two", reordered=True))

    assert first == second
    assert first.startswith("search_")
    assert "one" not in first
    assert "two" not in first


def test_v3_cache_write_and_fresh_stale_lookup_are_namespaced(tmp_path):
    cache = ResponseCacheV3(tmp_path)
    request = _request(request_id="current")

    entry_id = cache.put(request, _response_payload(), now=100)
    fresh = cache.get(request, ttl_seconds=20, allow_stale_seconds=40, now=110)
    stale = cache.get(request, ttl_seconds=20, allow_stale_seconds=40, now=130)
    expired = cache.get(request, ttl_seconds=20, allow_stale_seconds=40, now=161)

    assert fresh.disposition == "fresh_hit"
    assert fresh.payload is not None
    assert fresh.payload["origin_execution_id"] == "exec_stored"
    assert "request_id" not in fresh.payload
    assert "provider_attempts" not in fresh.payload
    assert "cache_status" not in fresh.payload
    assert fresh.age_seconds == 10
    assert stale.disposition == "stale_hit"
    assert stale.age_seconds == 30
    assert expired.disposition == "miss"
    path = tmp_path / "v3" / "response" / "search" / f"{entry_id}.json"
    envelope = json.loads(path.read_text()) if path.exists() else None
    # Expired owned entries may be removed; fresh/stale reads above prove the path.
    assert envelope is None or envelope["contract_version"] == "3.0"


def test_v3_clear_and_stats_never_touch_legacy_or_foreign_files(tmp_path):
    cache = ResponseCacheV3(tmp_path)
    request = _request(request_id="current")
    cache.put(request, _response_payload(), now=100)
    legacy = tmp_path / "legacy-v2.json"
    legacy.write_text('{"_cache_timestamp": 100}\n')
    foreign = tmp_path / "v3" / "response" / "foreign.json"
    foreign.parent.mkdir(parents=True, exist_ok=True)
    foreign.write_text('{"owner": "someone-else"}\n')

    before = cache.stats()
    cleared = cache.clear()
    after = cache.stats()

    assert before["entries"] == 1
    assert cleared == 1
    assert after["entries"] == 0
    assert legacy.exists()
    assert foreign.exists()


def test_corrupt_or_foreign_v3_entry_is_a_miss_and_not_deleted(tmp_path):
    cache = ResponseCacheV3(tmp_path)
    request = _request(request_id="current")
    path = cache.path_for(request)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"owner": "someone-else", "broken": true}\n')

    lookup = cache.get(request, ttl_seconds=20, allow_stale_seconds=0, now=110)

    assert lookup.disposition == "miss"
    assert path.exists()
