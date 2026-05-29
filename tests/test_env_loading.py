import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import search
import config


class EnvLoadingTests(unittest.TestCase):
    def test_load_env_file_reads_plugin_local_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin_dir = Path(tmp) / "web-search-plus"
            plugin_dir.mkdir()
            fake_script = plugin_dir / "search.py"
            fake_script.write_text("# fake")
            (plugin_dir / ".env").write_text("LINKUP_API_KEY=local-plugin-key\n")

            with mock.patch.object(search, "__file__", str(fake_script)):
                with mock.patch.dict(os.environ, {}, clear=True):
                    search._load_env_file()
                    self.assertEqual(os.environ.get("LINKUP_API_KEY"), "local-plugin-key")

    def test_load_env_file_ignores_template_placeholders(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin_dir = Path(tmp) / "web-search-plus"
            plugin_dir.mkdir()
            fake_script = plugin_dir / "search.py"
            fake_script.write_text("# fake")
            (plugin_dir / ".env").write_text("SERPER_API_KEY=***\nLINKUP_API_KEY=real-linkup-key\n")

            with mock.patch.object(search, "__file__", str(fake_script)):
                with mock.patch.dict(os.environ, {}, clear=True):
                    search._load_env_file()
                    self.assertIsNone(os.environ.get("SERPER_API_KEY"))
                    self.assertEqual(os.environ.get("LINKUP_API_KEY"), "real-linkup-key")

    def test_config_api_key_treats_template_placeholder_as_missing(self):
        with mock.patch.dict(os.environ, {"SERPER_API_KEY": "***"}, clear=True):
            self.assertIsNone(config.get_api_key("serper"))
            with self.assertRaises(config.ProviderConfigError) as exc:
                config.validate_api_key("serper")

        self.assertIn("Missing API key for serper", str(exc.exception))


if __name__ == "__main__":
    unittest.main()
