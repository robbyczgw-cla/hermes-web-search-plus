"""Config and state files are UTF-8 even where the locale encoding is not.

On Windows the locale encoding is usually cp1252, so a bare ``read_text()`` or
``write_text()`` decodes a UTF-8 file wrongly or fails on bytes cp1252 does not
map (0x81, 0x8d, 0x8f, 0x90, 0x9d). These tests make the default text encoding
cp1252 on every platform, so they also run on Linux CI.
"""

from __future__ import annotations

import io
import json

import pytest

from plugin_loader import load_plugin

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
