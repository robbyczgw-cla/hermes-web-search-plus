import provider_registry as registry
import config
import extract
import search
import __init__ as plugin


def test_provider_registry_is_the_complete_capability_source():
    assert registry.SEARCH_PROVIDER_IDS == (
        "serper",
        "serpbase",
        "brave",
        "tavily",
        "querit",
        "linkup",
        "exa",
        "firecrawl",
        "parallel",
        "perplexity",
        "kilo-perplexity",
        "you",
        "searxng",
    )
    assert registry.EXTRACT_PROVIDER_IDS == ("tavily", "exa", "linkup", "parallel", "firecrawl", "you")
    assert registry.PROVIDER_SPECS["serper"].env_var == "SERPER_API_KEY"
    assert registry.PROVIDER_SPECS["tavily"].supports_extract is True
    assert registry.PROVIDER_SPECS["brave"].auto_allowed_by_default is False
    assert "research" in registry.PROVIDER_SPECS["tavily"].capability_labels


def test_provider_registry_drives_config_extract_doctor_and_plugin_catalogs():
    registered = set(registry.PROVIDER_SPECS)

    assert set(config._ROUTING_PROVIDER_NAMES) == registered
    assert tuple(config.DEFAULT_CONFIG["auto_routing"]["provider_priority"]) == registry.DEFAULT_PROVIDER_PRIORITY
    assert config.DEFAULT_CONFIG["auto_routing"]["auto_allow"] == registry.DEFAULT_AUTO_ALLOW

    assert tuple(extract.EXTRACT_PROVIDER_PRIORITY) == registry.EXTRACT_PROVIDER_IDS
    assert search.PROVIDER_DOCTOR_CATALOG == registry.doctor_catalog()

    plugin_catalog = {item["provider"]: item for item in plugin._get_provider_catalog()}
    assert set(plugin_catalog) == registered
    for provider, spec in registry.PROVIDER_SPECS.items():
        item = plugin_catalog[provider]
        assert item["env"] == spec.env_var
        assert item["display_name"] == spec.display_name
        assert item["capabilities"] == list(spec.capability_labels)
        assert item["recommended"] == spec.recommended


def test_provider_argparse_choices_stay_in_registry_sync():
    parser = search.build_parser_for_tests()
    provider_action = next(action for action in parser._actions if "--provider" in action.option_strings)
    assert tuple(choice for choice in provider_action.choices if choice != "auto") == registry.SEARCH_PROVIDER_IDS
