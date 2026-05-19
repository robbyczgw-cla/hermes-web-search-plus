import json
import os
import subprocess
import sys
import unittest
from unittest import mock

import search
import __init__ as plugin


class ExtractPlusCoreTests(unittest.TestCase):
    def test_extract_firecrawl_parses_markdown(self):
        fake_response = {
            "success": True,
            "data": {
                "markdown": "# Example\nFirecrawl content",
                "html": "<h1>Example</h1>",
                "metadata": {"title": "Example Page", "sourceURL": "https://example.com"},
            },
        }
        with mock.patch("search.make_request", return_value=fake_response) as mock_request:
            result = search.extract_firecrawl(
                urls=["https://example.com"],
                api_key="fc-test",
                output_format="markdown",
            )

        self.assertEqual(result["provider"], "firecrawl")
        self.assertEqual(result["results"][0]["title"], "Example Page")
        self.assertEqual(result["results"][0]["content"], "# Example\nFirecrawl content")
        self.assertEqual(result["results"][0]["raw_content"], "# Example\nFirecrawl content")

        url, headers, body = mock_request.call_args.args[:3]
        self.assertEqual(url, "https://api.firecrawl.dev/v2/scrape")
        self.assertEqual(headers["Authorization"], "Bearer fc-test")
        self.assertEqual(body["url"], "https://example.com")
        self.assertEqual(body["formats"], ["markdown"])

    def test_extract_linkup_fetches_each_url(self):
        fake_response = {
            "markdown": "# Linkup page\nFetched content",
            "rawHtml": "<h1>Linkup page</h1>",
            "images": [{"alt": "Logo", "url": "https://example.com/logo.png"}],
        }
        with mock.patch("search.make_request", return_value=fake_response) as mock_request:
            result = search.extract_linkup(
                urls=["https://example.com"],
                api_key="linkup-test",
                output_format="markdown",
                include_images=True,
                include_raw_html=True,
                render_js=True,
            )

        self.assertEqual(result["provider"], "linkup")
        self.assertEqual(result["results"][0]["content"], "# Linkup page\nFetched content")
        self.assertEqual(result["results"][0]["raw_html"], "<h1>Linkup page</h1>")
        self.assertEqual(result["results"][0]["images"][0]["alt"], "Logo")

        url, headers, body = mock_request.call_args.args[:3]
        self.assertEqual(url, "https://api.linkup.so/v1/fetch")
        self.assertEqual(headers["Authorization"], "Bearer linkup-test")
        self.assertEqual(body["url"], "https://example.com")
        self.assertTrue(body["extractImages"])
        self.assertTrue(body["includeRawHtml"])
        self.assertTrue(body["renderJs"])

    def test_extract_tavily_parses_raw_content(self):
        fake_response = {
            "results": [
                {
                    "url": "https://example.com",
                    "title": "Tavily Page",
                    "raw_content": "Tavily extracted content",
                }
            ]
        }
        with mock.patch("search.make_request", return_value=fake_response) as mock_request:
            result = search.extract_tavily(
                urls=["https://example.com"],
                api_key="tvly-test",
            )

        self.assertEqual(result["provider"], "tavily")
        self.assertEqual(result["results"][0]["title"], "Tavily Page")
        self.assertEqual(result["results"][0]["content"], "Tavily extracted content")

        url, headers, body = mock_request.call_args.args[:3]
        self.assertEqual(url, "https://api.tavily.com/extract")
        self.assertEqual(headers["Authorization"], "Bearer tvly-test")
        self.assertEqual(body["urls"], ["https://example.com"])

    def test_extract_exa_parses_contents_text(self):
        fake_response = {
            "results": [
                {
                    "title": "Exa Page",
                    "url": "https://example.com",
                    "text": "Exa extracted markdown",
                    "summary": "Short summary",
                    "highlights": ["Important excerpt"],
                }
            ],
            "costDollars": {"total": 0.003},
        }
        with mock.patch("search.make_request", return_value=fake_response) as mock_request:
            result = search.extract_exa(
                urls=["https://example.com"],
                api_key="exa-test",
                output_format="markdown",
            )

        self.assertEqual(result["provider"], "exa")
        self.assertEqual(result["results"][0]["title"], "Exa Page")
        self.assertEqual(result["results"][0]["content"], "Exa extracted markdown")
        self.assertEqual(result["results"][0]["summary"], "Short summary")
        self.assertEqual(result["cost_dollars"], {"total": 0.003})

        url, headers, body = mock_request.call_args.args[:3]
        self.assertEqual(url, "https://api.exa.ai/contents")
        self.assertEqual(headers["x-api-key"], "exa-test")
        self.assertEqual(body["urls"], ["https://example.com"])
        self.assertTrue(body["text"])

    def test_extract_you_parses_contents_markdown(self):
        fake_response = [
            {
                "url": "https://example.com",
                "title": "You Page",
                "markdown": "You.com extracted markdown",
                "html": "<h1>You Page</h1>",
                "metadata": {"siteName": "Example"},
            }
        ]
        with mock.patch("search.make_request", return_value=fake_response) as mock_request:
            result = search.extract_you(
                urls=["https://example.com"],
                api_key="you-test",
                output_format="markdown",
                include_raw_html=True,
            )

        self.assertEqual(result["provider"], "you")
        self.assertEqual(result["results"][0]["title"], "You Page")
        self.assertEqual(result["results"][0]["content"], "You.com extracted markdown")
        self.assertEqual(result["results"][0]["raw_html"], "<h1>You Page</h1>")
        self.assertEqual(result["results"][0]["metadata"], {"siteName": "Example"})

        url, headers, body = mock_request.call_args.args[:3]
        self.assertEqual(url, "https://ydc-index.io/v1/contents")
        self.assertEqual(headers["X-API-Key"], "you-test")
        self.assertEqual(body["urls"], ["https://example.com"])
        self.assertEqual(body["formats"], ["markdown", "html", "metadata"])

    def test_extract_plus_auto_prefers_tavily_when_available(self):
        with mock.patch.dict(os.environ, {"TAVILY_API_KEY": "tvly-test", "FIRECRAWL_API_KEY": "fc-test", "LINKUP_API_KEY": "linkup-test"}, clear=False):
            with mock.patch("search.extract_tavily", return_value={"provider": "tavily", "results": []}) as mock_tavily:
                result = search.extract_plus(["https://example.com"], provider="auto")

        self.assertEqual(result["provider"], "tavily")
        mock_tavily.assert_called_once()

    def test_extract_plus_auto_uses_exa_when_only_exa_is_available(self):
        with mock.patch.dict(os.environ, {"EXA_API_KEY": "exa-test"}, clear=True):
            with mock.patch("search.extract_exa", return_value={"provider": "exa", "results": []}) as mock_exa:
                result = search.extract_plus(["https://example.com"], provider="auto")

        self.assertEqual(result["provider"], "exa")
        mock_exa.assert_called_once()

    def test_extract_provider_priority_prefers_fast_clean_extractors(self):
        self.assertEqual(search.EXTRACT_PROVIDER_PRIORITY, ["tavily", "exa", "linkup", "parallel", "firecrawl", "you"])

    def test_extract_plus_auto_prefers_tavily_over_exa(self):
        with mock.patch.dict(os.environ, {"EXA_API_KEY": "exa-test", "TAVILY_API_KEY": "tvly-test"}, clear=True):
            with mock.patch("search.extract_exa", return_value={"provider": "exa", "results": []}) as mock_exa:
                with mock.patch("search.extract_tavily", return_value={"provider": "tavily", "results": []}) as mock_tavily:
                    result = search.extract_plus(["https://example.com"], provider="auto")

        self.assertEqual(result["provider"], "tavily")
        mock_tavily.assert_called_once()
        mock_exa.assert_not_called()

    def test_extract_firecrawl_include_images_parses_markdown_and_og_image(self):
        fake_response = {
            "success": True,
            "data": {
                "markdown": "# Example\n![Hero](https://example.com/hero.png)\n![Hero again](https://example.com/hero.png)",
                "metadata": {"title": "Example Page", "sourceURL": "https://example.com", "ogImage": "https://example.com/og.png"},
            },
        }
        with mock.patch("search.make_request", return_value=fake_response):
            result = search.extract_firecrawl(
                urls=["https://example.com"],
                api_key="fc-test",
                include_images=True,
            )

        images = result["results"][0]["images"]
        self.assertEqual(images[0], {"alt": "og:image", "url": "https://example.com/og.png"})
        self.assertIn({"alt": "Hero", "url": "https://example.com/hero.png"}, images)

    def test_extract_plus_falls_back_when_primary_returns_only_errors(self):
        with mock.patch.dict(os.environ, {"FIRECRAWL_API_KEY": "fc-test", "LINKUP_API_KEY": "linkup-test"}, clear=True):
            with mock.patch("search.extract_firecrawl", return_value={"provider": "firecrawl", "results": [{"url": "https://example.com", "error": "fetch failed"}]}) as mock_firecrawl:
                with mock.patch("search.extract_linkup", return_value={"provider": "linkup", "results": [{"url": "https://example.com", "content": "fallback content"}]}) as mock_linkup:
                    result = search.extract_plus(["https://example.com"], provider="firecrawl")

        self.assertEqual(result["provider"], "linkup")
        self.assertTrue(result["routing"]["fallback_used"])
        self.assertEqual(result["routing"]["fallback_errors"][0]["error"], "all_urls_failed")
        mock_firecrawl.assert_called_once()
        mock_linkup.assert_called_once()

    def test_extract_plus_empty_urls_returns_clean_error_without_provider_calls(self):
        with mock.patch.dict(os.environ, {"FIRECRAWL_API_KEY": "fc-test"}, clear=True):
            with mock.patch("search.extract_firecrawl") as mock_firecrawl:
                result = search.extract_plus([], provider="firecrawl")

        self.assertEqual(result["results"], [])
        self.assertEqual(result["error"], "No URLs provided")
        mock_firecrawl.assert_not_called()

    def test_extract_plus_invalid_urls_return_clean_error_without_fallback(self):
        with mock.patch.dict(os.environ, {"FIRECRAWL_API_KEY": "fc-test", "LINKUP_API_KEY": "linkup-test"}, clear=True):
            with mock.patch("search.extract_firecrawl") as mock_firecrawl, mock.patch("search.extract_linkup") as mock_linkup:
                result = search.extract_plus(["foo-bar"], provider="firecrawl")

        self.assertEqual(result["results"], [])
        self.assertIn("Invalid URL(s)", result["error"])
        mock_firecrawl.assert_not_called()
        mock_linkup.assert_not_called()

    def test_cli_empty_extract_urls_returns_json_error(self):
        completed = subprocess.run(
            [sys.executable, "search.py", "--extract-urls", "--provider", "firecrawl", "--compact"],
            cwd=os.path.dirname(search.__file__),
            capture_output=True,
            text=True,
            timeout=30,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["results"], [])
        self.assertEqual(result["error"], "No URLs provided")

    def test_extract_plus_retries_transient_provider_errors(self):
        transient = search.ProviderRequestError("temporary outage", status_code=503, transient=True)
        with mock.patch.dict(os.environ, {"FIRECRAWL_API_KEY": "fc-test"}, clear=True):
            with mock.patch(
                "search.extract_firecrawl",
                side_effect=[transient, {"provider": "firecrawl", "results": [{"url": "https://example.com", "content": "ok"}]}],
            ) as mock_firecrawl:
                with mock.patch("search.time.sleep") as mock_sleep:
                    result = search.extract_plus(["https://example.com"], provider="firecrawl")

        self.assertEqual(result["provider"], "firecrawl")
        self.assertEqual(mock_firecrawl.call_count, 2)
        mock_sleep.assert_called_once_with(search.RETRY_BACKOFF_SECONDS[0])

    def test_extract_plus_marks_failed_provider_and_resets_successful_fallback(self):
        transient = search.ProviderRequestError("temporary outage", status_code=503, transient=True)
        with mock.patch.dict(os.environ, {"FIRECRAWL_API_KEY": "fc-test", "LINKUP_API_KEY": "linkup-test"}, clear=True):
            with mock.patch("search.extract_firecrawl", side_effect=[transient, transient, transient]):
                with mock.patch("search.extract_linkup", return_value={"provider": "linkup", "results": [{"url": "https://example.com", "content": "fallback"}]}):
                    with mock.patch("search.time.sleep"):
                        with mock.patch("search.mark_provider_failure", return_value={"cooldown_seconds": 60}) as mock_mark:
                            with mock.patch("search.reset_provider_health") as mock_reset:
                                result = search.extract_plus(["https://example.com"], provider="firecrawl")

        self.assertEqual(result["provider"], "linkup")
        mock_mark.assert_called_once()
        self.assertEqual(mock_mark.call_args.args[0], "firecrawl")
        mock_reset.assert_called_once_with("linkup")
        self.assertEqual(result["routing"]["fallback_errors"][0]["cooldown_seconds"], 60)

    def test_extract_plus_skips_provider_in_cooldown(self):
        def fake_cooldown(provider):
            return (provider == "tavily", 42 if provider == "tavily" else 0)

        with mock.patch.dict(os.environ, {"TAVILY_API_KEY": "tvly-test", "LINKUP_API_KEY": "linkup-test"}, clear=True):
            with mock.patch("search.provider_in_cooldown", side_effect=fake_cooldown):
                with mock.patch("search.extract_tavily") as mock_tavily:
                    with mock.patch("search.extract_linkup", return_value={"provider": "linkup", "results": [{"url": "https://example.com", "content": "fallback"}]}):
                        result = search.extract_plus(["https://example.com"], provider="auto")

        self.assertEqual(result["provider"], "linkup")
        mock_tavily.assert_not_called()
        self.assertEqual(result["routing"]["cooldown_skips"], [{"provider": "tavily", "cooldown_remaining_seconds": 42}])


class ExtractPlusPluginTests(unittest.TestCase):
    def test_run_extract_invokes_search_script_extract_mode(self):
        completed = mock.Mock(returncode=0, stdout=json.dumps({"provider": "linkup", "results": []}), stderr="")
        with mock.patch("subprocess.run", return_value=completed) as mock_run:
            result = plugin._run_extract(
                urls=["https://example.com"],
                provider="linkup",
                output_format="markdown",
                include_images=True,
                render_js=True,
            )

        self.assertEqual(result["provider"], "linkup")
        cmd = mock_run.call_args.kwargs["args"] if "args" in mock_run.call_args.kwargs else mock_run.call_args.args[0]
        self.assertIn("--extract-urls", cmd)
        self.assertIn("https://example.com", cmd)
        self.assertIn("--provider", cmd)
        self.assertIn("linkup", cmd)
        self.assertIn("--format", cmd)
        self.assertIn("markdown", cmd)
        self.assertIn("--extract-images", cmd)
        self.assertIn("--render-js", cmd)

    def test_register_exposes_web_extract_plus_tool(self):
        registered = {}

        class Ctx:
            def register_tool(self, **kwargs):
                registered[kwargs["name"]] = kwargs

        plugin.register(Ctx())

        self.assertIn("web_search_plus", registered)
        self.assertIn("web_extract_plus", registered)
        schema = registered["web_extract_plus"]["schema"]
        self.assertEqual(schema["parameters"]["required"], ["urls"])
        self.assertIn("firecrawl", schema["parameters"]["properties"]["provider"]["enum"])
        self.assertIn("linkup", schema["parameters"]["properties"]["provider"]["enum"])
        self.assertIn("exa", schema["parameters"]["properties"]["provider"]["enum"])
        self.assertIn("you", schema["parameters"]["properties"]["provider"]["enum"])

    def test_web_extract_plus_check_fn_requires_extract_capable_provider(self):
        registered = {}

        class Ctx:
            def register_tool(self, **kwargs):
                registered[kwargs["name"]] = kwargs

        with mock.patch.dict(os.environ, {"SERPER_API_KEY": "serper-test"}, clear=True):
            plugin.register(Ctx())
            self.assertTrue(registered["web_search_plus"]["check_fn"]())
            self.assertFalse(registered["web_extract_plus"]["check_fn"]())

        registered.clear()
        with mock.patch.dict(os.environ, {"FIRECRAWL_API_KEY": "fc-test"}, clear=True):
            plugin.register(Ctx())
            self.assertTrue(registered["web_extract_plus"]["check_fn"]())


if __name__ == "__main__":
    unittest.main()
