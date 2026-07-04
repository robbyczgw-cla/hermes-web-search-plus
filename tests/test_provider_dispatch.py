"""Registry/dispatch completeness and seam coverage for provider_dispatch.py.

These tests make a forgotten touchpoint structurally impossible: every
search/extract-capable provider in provider_registry.PROVIDER_SPECS must have
a dispatch adapter, no adapter may exist without a registry spec, the plugin
tool-schema enums must be derived from the registry, and adapters must keep
resolving provider functions late so module-level monkeypatches
(mock.patch.object(search, "search_you", ...)) keep working.
"""

import contextlib
import unittest
from unittest import mock

import __init__ as plugin
import provider_dispatch
import provider_registry
import search


class SearchDispatchCompletenessTests(unittest.TestCase):
    def test_every_search_capable_spec_has_a_search_dispatch_entry(self):
        search_capable = {
            spec.provider for spec in provider_registry.PROVIDER_SPECS.values() if spec.supports_search
        }
        missing = search_capable - set(provider_dispatch.SEARCH_DISPATCH)
        self.assertEqual(missing, set(), f"providers without SEARCH_DISPATCH adapter: {sorted(missing)}")

    def test_no_search_dispatch_entry_without_search_capable_spec(self):
        for provider in provider_dispatch.SEARCH_DISPATCH:
            spec = provider_registry.PROVIDER_SPECS.get(provider)
            self.assertIsNotNone(spec, f"SEARCH_DISPATCH entry {provider!r} has no registry spec")
            self.assertTrue(spec.supports_search, f"SEARCH_DISPATCH entry {provider!r} is not search-capable")

    def test_search_dispatch_matches_registry_search_provider_ids(self):
        self.assertEqual(set(provider_dispatch.SEARCH_DISPATCH), set(provider_registry.SEARCH_PROVIDER_IDS))


class ExtractDispatchCompletenessTests(unittest.TestCase):
    def test_every_extract_capable_spec_has_an_extract_dispatch_entry(self):
        extract_capable = {
            spec.provider for spec in provider_registry.PROVIDER_SPECS.values() if spec.supports_extract
        }
        missing = extract_capable - set(provider_dispatch.EXTRACT_DISPATCH)
        self.assertEqual(missing, set(), f"providers without EXTRACT_DISPATCH adapter: {sorted(missing)}")

    def test_no_extract_dispatch_entry_without_extract_capable_spec(self):
        for provider in provider_dispatch.EXTRACT_DISPATCH:
            spec = provider_registry.PROVIDER_SPECS.get(provider)
            self.assertIsNotNone(spec, f"EXTRACT_DISPATCH entry {provider!r} has no registry spec")
            self.assertTrue(spec.supports_extract, f"EXTRACT_DISPATCH entry {provider!r} is not extract-capable")

    def test_extract_dispatch_matches_registry_extract_provider_ids(self):
        self.assertEqual(set(provider_dispatch.EXTRACT_DISPATCH), set(provider_registry.EXTRACT_PROVIDER_IDS))


# Golden kwarg surface per provider, copied 1:1 from the pre-refactor if/elif
# chain in search.py execute_search(). Changing an adapter's kwargs is a
# behavior change and must show up here.
EXPECTED_SEARCH_KWARGS = {
    "serper": {"query", "api_key", "max_results", "country", "language", "search_type", "time_range", "include_images"},
    "serpbase": {"query", "api_key", "max_results", "country", "language", "page", "api_url", "timeout"},
    "brave": {"query", "api_key", "max_results", "country", "language", "time_range", "safesearch"},
    "tavily": {"query", "api_key", "max_results", "depth", "topic", "include_domains", "exclude_domains", "include_images", "include_raw_content"},
    "querit": {"query", "api_key", "max_results", "language", "country", "time_range", "include_domains", "exclude_domains", "base_url", "base_path", "timeout"},
    "linkup": {"query", "api_key", "max_results", "depth", "output_type", "include_domains", "exclude_domains", "api_url", "timeout"},
    "exa": {"query", "api_key", "max_results", "search_type", "exa_depth", "category", "start_date", "end_date", "similar_url", "include_domains", "exclude_domains", "text_verbosity"},
    "firecrawl": {"query", "api_key", "max_results", "country", "time_range", "sources", "include_domains", "exclude_domains", "scrape_markdown", "ignore_invalid_urls", "api_url", "timeout_ms"},
    "parallel": {"query", "api_key", "max_results", "include_domains", "exclude_domains", "api_url", "timeout", "client_model"},
    "perplexity": {"query", "api_key", "max_results", "model", "api_url", "freshness", "provider_name"},
    "kilo-perplexity": {"query", "api_key", "max_results", "model", "api_url", "freshness", "provider_name"},
    "you": {"query", "api_key", "max_results", "country", "language", "freshness", "safesearch", "include_news", "livecrawl"},
    "searxng": {"query", "instance_url", "max_results", "categories", "engines", "language", "time_range", "safesearch"},
    "keenable": {"query", "api_key", "max_results", "time_range", "include_domains", "public", "api_url", "timeout"},
}

# The provider function each search adapter must resolve (late) from the
# calling module's namespace.
EXPECTED_SEARCH_FUNCTION = {
    provider: "search_perplexity" if provider in ("perplexity", "kilo-perplexity") else "search_" + provider
    for provider in EXPECTED_SEARCH_KWARGS
}

EXPECTED_EXTRACT_KWARGS = {
    "firecrawl": {"api_url", "timeout"},
    "linkup": {"api_url", "timeout"},
    "tavily": {"api_url", "timeout"},
    "exa": {"api_url", "timeout"},
    "parallel": {"api_url", "timeout", "client_model", "max_chars_total", "max_chars_per_result"},
    "keenable": {"public", "api_url", "timeout"},
    "you": {"api_url", "timeout"},
    "serper": {"api_url", "timeout"},
}


class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return {"provider": "recorded", "query": "q", "results": [], "answer": "", "images": [], "metadata": {}}


class SearchAdapterKwargParityTests(unittest.TestCase):
    def test_adapters_build_the_same_kwargs_as_the_old_chain(self):
        config = search._deepcopy_default_config()
        parser = search.build_parser(config)
        args = parser.parse_args(["--query", "q"])
        for provider, adapter in sorted(provider_dispatch.SEARCH_DISPATCH.items()):
            recorder = _Recorder()
            namespace = {EXPECTED_SEARCH_FUNCTION[provider]: recorder}
            adapter(namespace, provider, args, "test-key" if provider != "searxng" else None, config, {})
            self.assertEqual(len(recorder.calls), 1, provider)
            call_args, call_kwargs = recorder.calls[0]
            self.assertEqual(call_args, (), provider)
            self.assertEqual(set(call_kwargs), EXPECTED_SEARCH_KWARGS[provider], provider)

    def test_exa_adapter_honours_routing_exa_depth_suggestion(self):
        config = search._deepcopy_default_config()
        args = search.build_parser(config).parse_args(["--query", "q"])
        recorder = _Recorder()
        provider_dispatch.SEARCH_DISPATCH["exa"]({"search_exa": recorder}, "exa", args, "k", config, {"exa_depth": "deep"})
        self.assertEqual(recorder.calls[0][1]["exa_depth"], "deep")


class ExtractAdapterKwargParityTests(unittest.TestCase):
    def test_adapters_build_the_same_kwargs_as_the_old_chain(self):
        config = search._deepcopy_default_config()
        for provider, adapter in sorted(provider_dispatch.EXTRACT_DISPATCH.items()):
            recorder = _Recorder()
            namespace = {"extract_" + provider: recorder}
            adapter(namespace, provider, ["https://example.com"], "test-key", "markdown", False, False, False, config, False)
            self.assertEqual(len(recorder.calls), 1, provider)
            call_args, call_kwargs = recorder.calls[0]
            self.assertEqual(call_args, (["https://example.com"], "test-key", "markdown", False, False, False), provider)
            self.assertEqual(set(call_kwargs), EXPECTED_EXTRACT_KWARGS[provider], provider)

    def test_parallel_adapter_uses_peer_level_default_full_content_budget(self):
        config = search._deepcopy_default_config()
        recorder = _Recorder()

        provider_dispatch.EXTRACT_DISPATCH["parallel"](
            {"extract_parallel": recorder},
            "parallel",
            ["https://example.com/long"],
            "test-key",
            "markdown",
            False,
            False,
            False,
            config,
            False,
        )

        kwargs = recorder.calls[0][1]
        # None = auto: extract_parallel scales the batch budget with len(urls).
        self.assertIsNone(kwargs["max_chars_total"])
        self.assertEqual(kwargs["max_chars_per_result"], 60000)

    def test_parallel_adapter_honours_explicit_cap_config(self):
        config = search._deepcopy_default_config()
        config["parallel"]["max_chars_total"] = 12000
        config["parallel"]["max_chars_per_result"] = 6000
        recorder = _Recorder()

        provider_dispatch.EXTRACT_DISPATCH["parallel"](
            {"extract_parallel": recorder},
            "parallel",
            ["https://example.com/long"],
            "test-key",
            "markdown",
            False,
            False,
            False,
            config,
            False,
        )

        kwargs = recorder.calls[0][1]
        self.assertEqual(kwargs["max_chars_total"], 12000)
        self.assertEqual(kwargs["max_chars_per_result"], 6000)


class DispatchMonkeypatchSeamTests(unittest.TestCase):
    """Dispatch must resolve provider functions late through search.py."""

    def _isolate(self, stack):
        stack.enter_context(mock.patch.object(search, "provider_in_cooldown", lambda p: (False, 0)))
        stack.enter_context(mock.patch.object(search, "cache_get", lambda **kw: None))
        stack.enter_context(mock.patch.object(search, "cache_put", lambda **kw: None))
        stack.enter_context(mock.patch.object(search, "reset_provider_health", lambda p: None))

    def test_search_dispatch_uses_module_level_monkeypatch(self):
        with contextlib.ExitStack() as stack:
            self._isolate(stack)
            stack.enter_context(mock.patch.dict("os.environ", {"KEENABLE_API_KEY": "keen-test-key"}))
            seen = {}

            def fake_keenable(**kwargs):
                seen.update(kwargs)
                return {"provider": "keenable", "query": "q", "results": [{"url": "https://example.test/a", "title": "A", "snippet": "s"}], "images": [], "answer": "", "metadata": {}}

            stack.enter_context(mock.patch.object(search, "search_keenable", fake_keenable))
            result = search.run_search_request(query="anything", provider="keenable", count=2)

        self.assertEqual(result["provider"], "keenable")
        # Adapter kwargs are sourced from the resolved keenable config section.
        self.assertIn("api_url", seen)
        self.assertIn("timeout", seen)
        self.assertIn("public", seen)

    def test_extract_dispatch_uses_module_level_monkeypatch(self):
        seen = {}

        def fake_tavily_extract(urls, key, *args, **kwargs):
            seen["urls"] = urls
            seen.update(kwargs)
            return {"provider": "tavily", "results": [{"url": urls[0], "title": "T", "content": "c", "raw_content": "c"}]}

        with mock.patch.dict("os.environ", {"TAVILY_API_KEY": "tavily-test-key"}):
            with mock.patch.object(search, "extract_tavily", fake_tavily_extract):
                result = search.extract_plus(["https://example.com"], provider="tavily")

        self.assertEqual(result["provider"], "tavily")
        self.assertEqual(seen["urls"], ["https://example.com"])
        self.assertIn("api_url", seen)
        self.assertIn("timeout", seen)


class ToolSchemaRegistryDerivationTests(unittest.TestCase):
    def test_tool_schema_provider_enums_are_registry_derived(self):
        registered = {}

        class Ctx:
            def register_tool(self, **kwargs):
                registered[kwargs["name"]] = kwargs

        plugin.register(Ctx())

        search_enum = registered["web_search_plus"]["schema"]["parameters"]["properties"]["provider"]["enum"]
        extract_enum = registered["web_extract_plus"]["schema"]["parameters"]["properties"]["provider"]["enum"]
        self.assertEqual(search_enum, ["auto", *provider_registry.SEARCH_PROVIDER_IDS])
        self.assertEqual(extract_enum, ["auto", *provider_registry.EXTRACT_PROVIDER_IDS])


if __name__ == "__main__":
    unittest.main()
