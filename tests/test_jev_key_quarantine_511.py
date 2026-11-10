"""A misplaced Jev credential must not discard unrelated operator settings."""
import json

from wsp_core import config


def test_jev_file_key_is_ignored_without_quarantining_settings(tmp_path, monkeypatch, caplog):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"version": 1, "default_provider": "brave", "jev": {"api_key": "jev-fixture-secret", "enabled": True}}))
    monkeypatch.setenv("WEB_SEARCH_PLUS_CONFIG", str(path))
    monkeypatch.setattr(config, "_IGNORED_FILE_KEY_WARNED", set())
    with caplog.at_level("WARNING"):
        for _ in range(2):
            loaded = config.load_config()
            assert loaded["default_provider"] == "brave"
            assert loaded["jev"]["enabled"] is True
            assert "api_key" not in loaded["jev"]
    assert path.exists()
    assert not list(tmp_path.glob("*.invalid*"))
    assert caplog.text.count("config.json contains an API key for jev") == 1
    assert "TYPESAFE_API_KEY" in caplog.text
    assert "jev-fixture-secret" not in caplog.text


def test_jev_key_file_in_config_is_named_once_and_keeps_settings(tmp_path, monkeypatch, caplog):
    # jev.api_key_file in config.json was accepted silently but never read:
    # Jev ran without a key and nothing said why.
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"default_provider": "brave", "jev": {"api_key_file": str(tmp_path / "k"), "enabled": True}}))
    monkeypatch.setenv("WEB_SEARCH_PLUS_CONFIG", str(path))
    monkeypatch.setattr(config, "_IGNORED_FILE_KEY_WARNED", set())
    with caplog.at_level("WARNING"):
        for _ in range(2):
            loaded = config.load_config()
            assert loaded["default_provider"] == "brave" and loaded["jev"]["enabled"] is True
    assert path.exists()
    assert caplog.text.count("config.json contains an API key for jev") == 1
    assert "TYPESAFE_API_KEY" in caplog.text
