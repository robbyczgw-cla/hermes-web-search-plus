import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cache


class CacheLifecycleTests(unittest.TestCase):
    def test_cache_stats_counts_json_and_web_text_cache(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            with mock.patch.object(cache, "CACHE_DIR", cache_dir):
                cache.cache_put("query", "linkup", 3, {"results": ["ok"]})
                first = cache.store_web_text("https://example.com/one", "alpha")
                second = cache.store_web_text("https://example.com/two", "bravo!")

                stats = cache.cache_stats()
                expected_web_bytes = Path(first["path"]).stat().st_size + Path(second["path"]).stat().st_size

        self.assertEqual(stats["total_entries"], 1)
        self.assertEqual(stats["web_text_entries"], 2)
        self.assertEqual(stats["web_text_size_bytes"], expected_web_bytes)
        self.assertEqual(stats["total_size_bytes_including_web"], stats["total_size_bytes"] + expected_web_bytes)
        self.assertTrue(stats["web_text_cache_dir"].endswith(f"{cache.WEB_TEXT_CACHE_DIRNAME}"))

    def test_cache_clear_removes_json_and_web_text_but_preserves_provider_health(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            with mock.patch.object(cache, "CACHE_DIR", cache_dir):
                cache.cache_put("query", "linkup", 3, {"results": ["ok"]})
                first = cache.store_web_text("https://example.com/one", "alpha")
                second = cache.store_web_text("https://example.com/two", "bravo!")
                provider_health = cache_dir / cache.PROVIDER_HEALTH_FILENAME
                provider_health.write_text('{"keep": true}\n', encoding="utf-8")

                result = cache.cache_clear()

                self.assertEqual(result["cleared"], 1)
                self.assertEqual(result["web_text_cleared"], 2)
                self.assertEqual(result["web_text_errors"], 0)
                self.assertGreater(result["size_freed_bytes"], 0)
                self.assertFalse(Path(first["path"]).exists())
                self.assertFalse(Path(second["path"]).exists())
                self.assertEqual(list(cache_dir.glob("*.json")), [provider_health])
                self.assertTrue(provider_health.exists())
                self.assertFalse(list((cache_dir / cache.WEB_TEXT_CACHE_DIRNAME).glob("*.md")))

    def test_cache_stats_absent_cache_dir_includes_web_text_zeros(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = Path(tmpdir) / "missing-cache"
            with mock.patch.object(cache, "CACHE_DIR", missing):
                stats = cache.cache_stats()

        self.assertFalse(stats["exists"])
        self.assertEqual(stats["total_entries"], 0)
        self.assertEqual(stats["web_text_entries"], 0)
        self.assertEqual(stats["web_text_size_bytes"], 0)
        self.assertEqual(stats["total_size_bytes_including_web"], 0)
        self.assertTrue(stats["web_text_cache_dir"].endswith(f"{cache.WEB_TEXT_CACHE_DIRNAME}"))

    def test_missing_web_subdir_stats_and_clear_are_zero(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            with mock.patch.object(cache, "CACHE_DIR", cache_dir):
                cache_dir.mkdir(exist_ok=True)
                stats = cache.cache_stats()
                result = cache.cache_clear()

        self.assertTrue(stats["exists"])
        self.assertEqual(stats["web_text_entries"], 0)
        self.assertEqual(stats["web_text_size_bytes"], 0)
        self.assertEqual(result["web_text_cleared"], 0)
        self.assertEqual(result["web_text_errors"], 0)

    def test_cache_stats_survives_foreign_json_files_in_cache_dir(self):
        """Regression: real cache dirs contain non-cache JSON state written by
        this plugin (provider_stats.json) or by hosts (usage_events.json as a
        bare list). cache_stats() used to call .get() on the list and crash."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            with mock.patch.object(cache, "CACHE_DIR", cache_dir):
                cache.cache_put("query", "linkup", 3, {"results": ["ok"]})
                (cache_dir / "usage_events.json").write_text('[{"event": "x"}]\n', encoding="utf-8")
                (cache_dir / "provider_stats.json").write_text('{"linkup": []}\n', encoding="utf-8")
                (cache_dir / "corrupt.json").write_text("{not json", encoding="utf-8")

                stats = cache.cache_stats()

        self.assertEqual(stats["total_entries"], 1)
        self.assertEqual(stats["providers"], {"linkup": 1})

    def test_cache_clear_preserves_foreign_json_state(self):
        """Clearing the cache must delete only marker-carrying cache entries,
        never foreign state files that happen to live in the cache dir."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            with mock.patch.object(cache, "CACHE_DIR", cache_dir):
                cache.cache_put("query", "linkup", 3, {"results": ["ok"]})
                usage = cache_dir / "usage_events.json"
                usage.write_text('[{"event": "x"}]\n', encoding="utf-8")
                stats_file = cache_dir / "provider_stats.json"
                stats_file.write_text('{"linkup": []}\n', encoding="utf-8")

                result = cache.cache_clear()

                self.assertEqual(result["cleared"], 1)
                self.assertTrue(usage.exists())
                self.assertTrue(stats_file.exists())

    def test_web_text_stats_ignore_but_clear_orphan_tmp_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            web_dir = cache_dir / cache.WEB_TEXT_CACHE_DIRNAME
            web_dir.mkdir(parents=True)
            orphan = web_dir / "abc.md.tmp"
            orphan.write_text("orphan", encoding="utf-8")
            with mock.patch.object(cache, "CACHE_DIR", cache_dir):
                stats = cache.cache_stats()
                result = cache.cache_clear()

        self.assertEqual(stats["web_text_entries"], 0)
        self.assertEqual(stats["web_text_size_bytes"], 0)
        self.assertEqual(result["web_text_cleared"], 0)
        self.assertEqual(result["web_text_tmp_cleared"], 1)
        self.assertFalse(orphan.exists())


if __name__ == "__main__":
    unittest.main()
