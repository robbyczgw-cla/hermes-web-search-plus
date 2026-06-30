import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import search
import cache
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

    def test_extract_plus_auto_skips_disabled_providers(self):
        config = {
            "auto_routing": {
                "disabled_providers": ["firecrawl"],
                "provider_priority": ["tavily", "exa", "linkup", "parallel", "firecrawl", "you"],
            }
        }
        with mock.patch.dict(os.environ, {"TAVILY_API_KEY": "tvly-test", "FIRECRAWL_API_KEY": "fc-test"}, clear=True):
            with mock.patch("search.extract_tavily", return_value={"provider": "tavily", "results": []}) as mock_tavily:
                with mock.patch("search.extract_firecrawl") as mock_firecrawl:
                    result = search.extract_plus(["https://example.com"], provider="auto", config=config)

        self.assertEqual(result["provider"], "tavily")
        mock_tavily.assert_called_once()
        mock_firecrawl.assert_not_called()

    def test_extract_plus_explicit_disabled_provider_is_still_tried(self):
        config = {
            "auto_routing": {
                "disabled_providers": ["firecrawl"],
                "provider_priority": ["tavily", "exa", "linkup", "parallel", "firecrawl", "you"],
            }
        }
        with mock.patch.dict(os.environ, {"FIRECRAWL_API_KEY": "fc-test", "LINKUP_API_KEY": "linkup-test"}, clear=True):
            with mock.patch("search.extract_firecrawl", return_value={"provider": "firecrawl", "results": [{"url": "https://example.com", "error": "fetch failed"}]}) as mock_firecrawl:
                with mock.patch("search.extract_linkup", return_value={"provider": "linkup", "results": [{"url": "https://example.com", "content": "fallback"}]}) as mock_linkup:
                    result = search.extract_plus(["https://example.com"], provider="firecrawl", config=config)

        self.assertEqual(result["provider"], "linkup")
        mock_firecrawl.assert_called_once()
        mock_linkup.assert_called_once()

    def test_extract_provider_priority_prefers_fast_clean_extractors(self):
        self.assertEqual(search.EXTRACT_PROVIDER_PRIORITY, ["tavily", "exa", "linkup", "parallel", "firecrawl", "you", "keenable"])

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
        # Retry backoff now carries jitter to de-synchronize retries: the delay is
        # the base step plus up to RETRY_JITTER_FRACTION of it.
        mock_sleep.assert_called_once()
        (slept,) = mock_sleep.call_args[0]
        base = search.RETRY_BACKOFF_SECONDS[0]
        self.assertGreaterEqual(slept, base)
        self.assertLessEqual(slept, base * (1 + search.RETRY_JITTER_FRACTION))

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
    def test_run_extract_runs_in_process(self):
        captured = {}

        def fake_run_extract_request(urls, **kwargs):
            captured["urls"] = urls
            captured.update(kwargs)
            return {"provider": "linkup", "results": []}

        class FakeSearch:
            run_extract_request = staticmethod(fake_run_extract_request)

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WSP_FORCE_SUBPROCESS", None)
            with mock.patch.object(plugin, "_load_search_module", return_value=FakeSearch):
                with mock.patch("subprocess.run", side_effect=AssertionError("should not subprocess")):
                    result = plugin._run_extract(
                        urls=["https://example.com"],
                        provider="linkup",
                        output_format="markdown",
                        include_images=True,
                        render_js=True,
                    )

        self.assertEqual(result["provider"], "linkup")
        self.assertEqual(captured["urls"], ["https://example.com"])
        self.assertEqual(captured["provider"], "linkup")
        self.assertEqual(captured["output_format"], "markdown")
        self.assertTrue(captured["include_images"])
        self.assertTrue(captured["render_js"])

    def test_run_extract_subprocess_fallback_builds_extract_command(self):
        completed = mock.Mock(returncode=0, stdout=json.dumps({"provider": "linkup", "results": []}), stderr="")
        with mock.patch.dict(os.environ, {"WSP_FORCE_SUBPROCESS": "1"}):
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


    def test_format_extract_results_short_content_returns_full_without_store(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(cache, "CACHE_DIR", Path(tmpdir)):
                with mock.patch.object(plugin, "load_config", return_value={"web": {"extract_char_limit": 15000}}):
                    output = plugin._format_extract_results({
                        "provider": "linkup",
                        "results": [{"title": "Short", "url": "https://example.com/short", "content": "short page\nall here"}],
                    })

            self.assertIn("short page\nall here", output)
            self.assertNotIn("Content truncated", output)
            self.assertFalse((Path(tmpdir) / "web").exists())

    def test_format_extract_results_long_content_truncates_stores_and_reports_line_offset(self):
        lines = [f"line {i:04d} " + ("x" * 90) for i in range(350)]
        content = "\n".join(lines)
        limit = 15000
        expected_head = content[: int(limit * 2 / 3)].rstrip()
        expected_offset = expected_head.count("\n") + 1

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(cache, "CACHE_DIR", Path(tmpdir)):
                with mock.patch.object(plugin, "load_config", return_value={"web": {"extract_char_limit": limit}}):
                    output = plugin._format_extract_results({
                        "provider": "linkup",
                        "results": [{"title": "Long", "url": "https://example.com/long", "content": content}],
                    })
            stored_files = list((Path(tmpdir) / "web").glob("*.md"))
            self.assertEqual(len(stored_files), 1)
            stored = stored_files[0].read_text(encoding="utf-8")

        self.assertIn("Content truncated", output)
        self.assertIn(str(stored_files[0]), output)
        self.assertIn(f"offset={expected_offset}", output)
        self.assertIn("limit=500", output)
        self.assertIn("[... omitted middle; see footer for page-on-demand ...]", output)
        self.assertEqual(stored, content)
        self.assertLess(len(output), len(content))

    def test_format_extract_results_stored_text_cap_sets_marker(self):
        content = "A" * (cache.MAX_STORED_TEXT_CHARS + 1234)
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(cache, "CACHE_DIR", Path(tmpdir)):
                with mock.patch.object(plugin, "load_config", return_value={"web": {"extract_char_limit": 2000}}):
                    output = plugin._format_extract_results({
                        "provider": "linkup",
                        "results": [{"title": "Huge", "url": "https://example.com/huge", "content": content}],
                    })
            stored_files = list((Path(tmpdir) / "web").glob("*.md"))
            self.assertEqual(len(stored_files), 1)
            stored = stored_files[0].read_text(encoding="utf-8")

        self.assertIn("Stored file capped at 2000000 characters", output)
        self.assertIn("[TRUNCATED: stored text capped at 2000000 characters]", stored)
        self.assertLess(len(stored), len(content) + 100)

    def test_format_extract_results_replaces_base64_images_but_keeps_https_images(self):
        content = (
            "Intro\n"
            "![Tiny](data:image/png;base64," + ("a" * 80) + ")\n"
            "![Hero](https://example.com/hero.png)\n"
            "<img alt=\"Inline\" src=\"data:image/jpeg;base64,bbbb\">"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(cache, "CACHE_DIR", Path(tmpdir)):
                with mock.patch.object(plugin, "load_config", return_value={"web": {"extract_char_limit": 15000}}):
                    output = plugin._format_extract_results({
                        "provider": "linkup",
                        "results": [{"title": "Images", "url": "https://example.com/images", "content": content}],
                    })

        self.assertIn("[IMAGE: Tiny]", output)
        self.assertIn("[IMAGE: Inline]", output)
        self.assertIn("![Hero](https://example.com/hero.png)", output)
        self.assertNotIn("data:image", output)

    def test_extract_char_limit_invalid_config_falls_back_and_small_limit_floors(self):
        with mock.patch.object(plugin, "load_config", return_value={"web": {"extract_char_limit": "nope"}}):
            self.assertEqual(plugin._extract_char_limit(), 15000)

        with mock.patch.object(plugin, "load_config", return_value={"web": {"extract_char_limit": 12}}):
            self.assertEqual(plugin._extract_char_limit(), 1000)

        with mock.patch.object(plugin, "load_config", side_effect=RuntimeError("bad config")):
            self.assertEqual(plugin._extract_char_limit(), 15000)

    def test_store_web_text_path_is_deterministic_and_separate_from_json_cache(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(cache, "CACHE_DIR", Path(tmpdir)):
                first = cache.store_web_text("https://example.com/same", "alpha")
                second = cache.store_web_text("https://example.com/same", "beta")
                other = cache.store_web_text("https://example.com/other", "gamma")

                self.assertEqual(first["path"], second["path"])
                self.assertNotEqual(first["path"], other["path"])
                self.assertIn(f"{os.sep}web{os.sep}", first["path"])
                self.assertTrue(first["path"].endswith(".md"))
                self.assertEqual(Path(first["path"]).read_text(encoding="utf-8"), "beta")
                self.assertFalse(list(Path(tmpdir).glob("*.json")))

    def test_format_extract_results_store_failure_preserves_preview_and_reports_path(self):
        content = "\n".join(f"line {i} " + ("x" * 80) for i in range(80))
        with mock.patch.object(plugin, "load_config", return_value={"web": {"extract_char_limit": 1000}}):
            with mock.patch.object(plugin, "store_web_text", return_value={
                "stored": False,
                "path": "/tmp/unwritable/web/example.md",
                "capped": False,
                "original_chars": len(content),
                "stored_chars": 0,
                "error": "permission denied",
            }):
                output = plugin._format_extract_results({
                    "provider": "linkup",
                    "results": [{"title": "Fail", "url": "https://example.com/fail", "content": content}],
                })

        self.assertIn("Content truncated", output)
        self.assertIn("Full-text store failed", output)
        self.assertIn("permission denied", output)
        self.assertIn("line 0", output)

    def test_format_extract_results_uses_raw_content_when_content_missing(self):
        content = "raw " * 500
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(cache, "CACHE_DIR", Path(tmpdir)):
                with mock.patch.object(plugin, "load_config", return_value={"web": {"extract_char_limit": 1000}}):
                    output = plugin._format_extract_results({
                        "provider": "firecrawl",
                        "results": [{"title": "Raw", "url": "https://example.com/raw", "raw_content": content}],
                    })

        self.assertIn("Raw", output)
        self.assertIn("Content truncated", output)
        self.assertIn("read_file(path=", output)

    def test_format_extract_results_multiline_base64_and_html_http_image(self):
        content = (
            "before\n"
            "![Wrapped](data:image/png;base64,aaaa\nbbbb\ncccc)\n"
            '<img src="https://example.com/ok.jpg" alt="Remote">\n'
            "after"
        )
        with mock.patch.object(plugin, "load_config", return_value={"web": {"extract_char_limit": 15000}}):
            output = plugin._format_extract_results({
                "provider": "linkup",
                "results": [{"title": "Wrapped", "url": "https://example.com/wrapped", "content": content}],
            })

        self.assertIn("[IMAGE: Wrapped]", output)
        self.assertIn('src="https://example.com/ok.jpg"', output)
        self.assertNotIn("data:image", output)

    def test_format_extract_results_read_file_offset_lands_in_omitted_middle(self):
        lines = [f"headline {i:03d} " + ("x" * 40) for i in range(140)]
        content = "\n".join(lines)
        limit = 1000
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(cache, "CACHE_DIR", Path(tmpdir)):
                with mock.patch.object(plugin, "load_config", return_value={"web": {"extract_char_limit": limit}}):
                    output = plugin._format_extract_results({
                        "provider": "linkup",
                        "results": [{"title": "Offset", "url": "https://example.com/offset", "content": content}],
                    })
                stored_file = next((Path(tmpdir) / "web").glob("*.md"))
                stored_lines = stored_file.read_text(encoding="utf-8").splitlines()

        expected_head = content[: int(limit * 2 / 3)].rstrip()
        expected_offset = expected_head.count("\n") + 1
        self.assertIn(f"offset={expected_offset}", output)
        self.assertGreaterEqual(expected_offset, 1)
        self.assertLessEqual(expected_offset, len(stored_lines))
        first_paged_line = stored_lines[expected_offset - 1]
        self.assertNotIn(first_paged_line, expected_head.splitlines()[:-1])
        self.assertIn(first_paged_line, content)

    def test_format_extract_results_two_long_results_store_two_files(self):
        one = "one\n" * 800
        two = "two\n" * 800
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(cache, "CACHE_DIR", Path(tmpdir)):
                with mock.patch.object(plugin, "load_config", return_value={"web": {"extract_char_limit": 1000}}):
                    output = plugin._format_extract_results({
                        "provider": "linkup",
                        "results": [
                            {"title": "One", "url": "https://example.com/one", "content": one},
                            {"title": "Two", "url": "https://example.com/two", "content": two},
                        ],
                    })
                stored_files = list((Path(tmpdir) / "web").glob("*.md"))

        self.assertEqual(len(stored_files), 2)
        self.assertEqual(output.count("Content truncated"), 2)
        self.assertIn("1. One", output)
        self.assertIn("2. Two", output)

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

    def test_web_extract_plus_opt_in_enables_keyless_keenable(self):
        registered = {}

        class Ctx:
            def register_tool(self, **kwargs):
                registered[kwargs["name"]] = kwargs

        with mock.patch.dict(os.environ, {"KEENABLE_ALLOW_PUBLIC": "1"}, clear=True):
            plugin.register(Ctx())
            self.assertTrue(registered["web_search_plus"]["check_fn"]())
            self.assertTrue(registered["web_extract_plus"]["check_fn"]())


if __name__ == "__main__":
    unittest.main()
