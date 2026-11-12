"""Config and state files are UTF-8 even where the locale encoding is not.

On Windows the locale encoding is usually cp1252, so a bare ``read_text()`` or
``write_text()`` decodes a UTF-8 file wrongly or fails on bytes cp1252 does not
map (0x81, 0x8d, 0x8f, 0x90, 0x9d). These tests make the default text encoding
cp1252 on every platform, so they also run on Linux CI.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from plugin_loader import load_plugin
from wsp_core import config as wsp_config
from wsp_core import env_loader

wsp = load_plugin("wsp_plugin_text_encoding_under_test")

# U+038F (UTF-8 CE 8F) and U+201D (E2 80 9D) carry bytes cp1252 cannot decode.
NON_CP1252 = "Ώ \u2014 \u201cquoted\u201d caf\u00e9"


@pytest.fixture(autouse=True)
def _cp1252_locale(monkeypatch):
    real = io.text_encoding

    def text_encoding(encoding, stacklevel=2):
        return "cp1252" if encoding is None else real(encoding)

    monkeypatch.setattr(io, "text_encoding", text_encoding)
    with pytest.raises(UnicodeDecodeError):
        NON_CP1252.encode("utf-8").decode(io.text_encoding(None))


def test_behavior_config_with_utf8_text_loads(tmp_path):
    path = tmp_path / "config.json"
    path.write_bytes(json.dumps({"searxng": {"note": NON_CP1252}}, ensure_ascii=False).encode("utf-8"))

    loaded = wsp._load_behavior_config(path)

    assert loaded["searxng"]["note"] == NON_CP1252
    assert path.exists(), "a valid UTF-8 config must not be quarantined"


def test_behavior_config_with_bom_loads(tmp_path):
    path = tmp_path / "config.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"searxng": {"note": NON_CP1252}}, ensure_ascii=False).encode("utf-8"))

    assert wsp._load_behavior_config(path)["searxng"]["note"] == NON_CP1252
    assert path.exists()


def test_behavior_config_write_keeps_utf8_values_byte_exact(tmp_path):
    path = tmp_path / "config.json"
    path.write_bytes(json.dumps({"searxng": {"note": NON_CP1252}}, ensure_ascii=False).encode("utf-8"))

    wsp._write_behavior_config(path, {"auto_routing": {"enabled": False}})

    raw = path.read_bytes()
    data = json.loads(raw.decode("utf-8"))
    assert data["searxng"]["note"] == NON_CP1252
    assert data["auto_routing"]["enabled"] is False
    assert "\ufffd" not in raw.decode("utf-8")


def test_invalid_utf8_behavior_config_is_quarantined_not_replaced(tmp_path, capsys):
    path = tmp_path / "config.json"
    path.write_bytes(b'{"searxng": {"note": "\xff\xfe"}}')

    loaded = wsp._load_behavior_config(path)

    assert loaded == wsp._default_behavior_config()
    assert not path.exists()
    assert "invalid config moved" in capsys.readouterr().err


def test_hermes_config_yaml_with_utf8_text_is_read(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_bytes(f"# {NON_CP1252}\nweb:\n  keyless_rescue: false\n".encode("utf-8"))

    assert wsp._hermes_keyless_rescue_state(config)["keyless_rescue"] is False


def test_onboarding_state_with_utf8_text_is_read_and_written_as_utf8(tmp_path):
    shown = tmp_path / "shown.json"
    shown.write_bytes(json.dumps({"unconfigured_hint_shown": True, "note": NON_CP1252}, ensure_ascii=False).encode("utf-8"))
    assert wsp._unconfigured_session_hint(env={}, state_path=shown) is None

    fresh = tmp_path / "fresh.json"
    assert wsp._unconfigured_session_hint(env={}, state_path=fresh) is not None
    assert json.loads(fresh.read_bytes().decode("utf-8")) == {"unconfigured_hint_shown": True}


@pytest.mark.parametrize("bom", [b"", b"\xef\xbb\xbf"], ids=["utf8", "utf8-bom"])
def test_engine_load_config_reads_utf8_config_json_without_quarantine(tmp_path, monkeypatch, bom):
    path = tmp_path / "config.json"
    path.write_bytes(bom + json.dumps({"searxng": {"note": NON_CP1252}}, ensure_ascii=False).encode("utf-8"))
    monkeypatch.setenv("WEB_SEARCH_PLUS_CONFIG", str(path))

    loaded = wsp_config.load_config()

    assert loaded["searxng"]["note"] == NON_CP1252
    assert path.exists(), "a valid UTF-8 config must not be quarantined"
    assert not list(tmp_path.glob("config.json.broken-*"))


def test_onboarding_state_with_bom_is_read(tmp_path):
    shown = tmp_path / "shown.json"
    shown.write_bytes(b"\xef\xbb\xbf" + json.dumps({"unconfigured_hint_shown": True}).encode("utf-8"))

    assert wsp._unconfigured_session_hint(env={}, state_path=shown) is None


def test_hermes_config_yaml_with_bom_is_read(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_bytes(b"\xef\xbb\xbf" + f"web:  # {NON_CP1252}\n  keyless_rescue: false\n".encode("utf-8"))

    assert wsp._hermes_keyless_rescue_state(config)["keyless_rescue"] is False


def test_env_file_with_bom_and_utf8_comment_is_read(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_bytes(b"\xef\xbb\xbf" + f"WSP_BOM_FIRST=one\n# {NON_CP1252}\nWSP_AFTER=two\n".encode("utf-8"))
    monkeypatch.setattr(env_loader, "candidate_env_paths", lambda anchor: [env_path])
    target: dict = {}

    env_loader.load_env_files(tmp_path / "anchor.py", target)

    assert target == {"WSP_BOM_FIRST": "one", "WSP_AFTER": "two"}


def test_standalone_setup_config_prints_under_a_cp1252_console(tmp_path):
    """`setup.py config ...` prints a check mark; a cp1252 stdout cannot encode it."""
    script = Path(__file__).resolve().parents[1] / "setup.py"
    config_path = tmp_path / "config.json"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "HERMES_HOME": str(tmp_path / "hermes"),
        "PYTHONUTF8": "0",
        "PYTHONIOENCODING": "cp1252",
    }
    if "SYSTEMROOT" in os.environ:
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]

    result = subprocess.run(
        [sys.executable, str(script), "config", "set-routing", "off", "--config-path", str(config_path)],
        env=env,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert "\u2713 Updated routing config" in result.stdout.decode("utf-8")
    assert json.loads(config_path.read_bytes().decode("utf-8"))["auto_routing"]["enabled"] is False
