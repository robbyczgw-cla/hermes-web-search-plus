import importlib.util
from pathlib import Path

SEARCH_PATH = Path(__file__).resolve().parents[1] / "search.py"
search_spec = importlib.util.spec_from_file_location("wsp_search_shim_policy_under_test", SEARCH_PATH)
assert search_spec is not None
search = importlib.util.module_from_spec(search_spec)
assert search_spec.loader is not None
search_spec.loader.exec_module(search)


def test_compatibility_shim_policy_documents_public_surface_and_removal_path():
    policy = search.get_compatibility_shim_policy()

    assert policy["tracking_issue"] == "#34"
    assert "ProviderSpec registry" in policy["removal_target"]
    assert "one documented minor release window" in policy["removal_target"]
    assert "v2.3" not in policy["removal_target"]
    assert "QueryAnalyzer" in policy["public_surface"]
    assert "extract_plus" in policy["public_surface"]
    assert "extract_url_content" not in policy["public_surface"]
    assert "_sync_provider_dependencies" in policy["internal_shims"]
    assert "do not remove wrappers" in policy["policy"]

    policy["public_surface"].append("mutated")
    assert "mutated" not in search.get_compatibility_shim_policy()["public_surface"]
