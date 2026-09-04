import search

import pytest


PUBLIC_EXPORTS = (
    "QueryAnalyzer", "auto_route_provider", "extract_plus",
    "get_cached_result", "cache_search_result", "clear_cache", "get_cache_stats",
)


def test_legacy_public_exports_remain_callable():
    for name in PUBLIC_EXPORTS:
        assert callable(getattr(search, name)), name
    assert set(search.get_compatibility_shim_policy()["public_surface"]) == set(PUBLIC_EXPORTS)


@pytest.mark.parametrize("field", ["public_surface", "internal_shims"])
def test_policy_lists_are_defensive_copies(field):
    policy = search.get_compatibility_shim_policy()
    original = policy[field].copy()
    policy[field].append("mutated")
    assert search.get_compatibility_shim_policy()[field] == original
