"""env_loader stays a utility for hosts that vendor wsp_core (MCP server, OpenClaw skill).

The Hermes plugin and the engine never call it; tests/test_secret_scope.py
checks that no .env file is opened. These tests call ``load_env_files``
directly with the anchor file a host would pass.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from wsp_core import config
from wsp_core import env_loader


def _hermes_profile_env(env_file: Path):
    """The profile .env Hermes reports (via hermes_constants, as in a real Hermes)."""
    home = env_file.parent if env_file.name == ".env" else env_file.with_suffix("")
    return mock.patch.dict(sys.modules, {"hermes_constants": SimpleNamespace(get_hermes_home=lambda: home)})


class EnvLoadingTests(unittest.TestCase):
    def test_load_env_file_reads_plugin_local_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin_dir = Path(tmp) / "web-search-plus"
            plugin_dir.mkdir()
            fake_script = plugin_dir / "search.py"
            fake_script.write_text("# fake")
            (plugin_dir / ".env").write_text("LINKUP_API_KEY=local-plugin-key\n")

            with _hermes_profile_env(Path(tmp) / "missing-hermes.env"):
                with mock.patch.dict(os.environ, {}, clear=True):
                    env_loader.load_env_files(fake_script)
                    self.assertEqual(os.environ.get("LINKUP_API_KEY"), "local-plugin-key")

    def test_load_env_file_ignores_template_placeholders(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin_dir = Path(tmp) / "web-search-plus"
            plugin_dir.mkdir()
            fake_script = plugin_dir / "search.py"
            fake_script.write_text("# fake")
            (plugin_dir / ".env").write_text("SERPER_API_KEY=***\nLINKUP_API_KEY=real-linkup-key\n")

            with _hermes_profile_env(Path(tmp) / "missing-hermes.env"):
                with mock.patch.dict(os.environ, {}, clear=True):
                    env_loader.load_env_files(fake_script)
                    self.assertIsNone(os.environ.get("SERPER_API_KEY"))
                    self.assertEqual(os.environ.get("LINKUP_API_KEY"), "real-linkup-key")

    def test_load_env_files_reads_hermes_profile_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin_dir = Path(tmp) / "web-search-plus"
            plugin_dir.mkdir()
            fake_script = plugin_dir / "search.py"
            fake_script.write_text("# fake")
            hermes_env = Path(tmp) / "hermes-home" / ".env"
            hermes_env.parent.mkdir()
            hermes_env.write_text("TAVILY_API_KEY=tvly-from-hermes\n")

            with _hermes_profile_env(hermes_env):
                with mock.patch.dict(os.environ, {}, clear=True):
                    env_loader.load_env_files(fake_script)
                    self.assertEqual(os.environ.get("TAVILY_API_KEY"), "tvly-from-hermes")

    def test_load_env_files_reads_hermes_profile_env_for_any_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin_dir = Path(tmp) / "web-search-plus"
            plugin_dir.mkdir()
            fake_config = plugin_dir / "config.py"
            fake_config.write_text("# fake")
            hermes_env = Path(tmp) / "hermes-home" / ".env"
            hermes_env.parent.mkdir()
            hermes_env.write_text("EXA_API_KEY=exa-from-hermes\n")

            with _hermes_profile_env(hermes_env):
                with mock.patch.dict(os.environ, {}, clear=True):
                    env_loader.load_env_files(fake_config)
                    self.assertEqual(os.environ.get("EXA_API_KEY"), "exa-from-hermes")

    def test_hermes_profile_env_does_not_override_existing_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin_dir = Path(tmp) / "web-search-plus"
            plugin_dir.mkdir()
            fake_script = plugin_dir / "search.py"
            fake_script.write_text("# fake")
            hermes_env = Path(tmp) / "hermes-home" / ".env"
            hermes_env.parent.mkdir()
            hermes_env.write_text("TAVILY_API_KEY=tvly-from-hermes\n")

            with _hermes_profile_env(hermes_env):
                with mock.patch.dict(os.environ, {"TAVILY_API_KEY": "already-set"}, clear=True):
                    env_loader.load_env_files(fake_script)
                    self.assertEqual(os.environ.get("TAVILY_API_KEY"), "already-set")

    def test_config_api_key_treats_template_placeholder_as_missing(self):
        with mock.patch.dict(os.environ, {"SERPER_API_KEY": "***"}, clear=True):
            self.assertIsNone(config.get_api_key("serper"))
            with self.assertRaises(config.ProviderConfigError) as exc:
                config.validate_api_key("serper")

        self.assertIn("Missing API key for serper", str(exc.exception))


if __name__ == "__main__":
    unittest.main()
