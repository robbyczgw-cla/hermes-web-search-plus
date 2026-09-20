from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from jev_setup import JEV_DECISIONS, apply_jev_config, parse_decisions, persist_key_file, status_payload

PLUGIN_PATH = Path(__file__).resolve().parents[1] / "__init__.py"
spec = importlib.util.spec_from_file_location("wsp_plugin_jev_setup_under_test", PLUGIN_PATH)
wsp = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(wsp)


def test_parse_decisions_default_is_three():
    assert parse_decisions(None) == JEV_DECISIONS
    assert parse_decisions("") == JEV_DECISIONS
    assert JEV_DECISIONS == ("search_type", "extract_quality", "language_fill")


def test_parse_decisions_rejects_unknown():
    try:
        parse_decisions("extract_quality,cache")
    except ValueError as exc:
        assert "cache" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_status_payload_never_includes_secret(tmp_path, monkeypatch):
    env = {"TYPESAFE_API_KEY": "apikey_should_not_leak"}
    payload = status_payload(env, {"jev": {"enabled": True, "extract_quality": True}})
    blob = json.dumps(payload)
    assert "apikey_should_not_leak" not in blob
    assert payload["key_present"] is True
    assert payload["key_source"] == "env"
    assert payload["enabled"] is True


def test_status_payload_reads_key_file_without_leaking(tmp_path):
    secret = tmp_path / "typesafe_api_key"
    secret.write_text("file-secret-value\n", encoding="utf-8")
    env = {"TYPESAFE_API_KEY_FILE": str(secret)}
    payload = status_payload(env, {})
    assert payload["key_present"] is True
    assert payload["key_source"] == "file"
    assert "file-secret-value" not in json.dumps(payload)


def test_apply_jev_config_strips_api_key_field():
    config = apply_jev_config(
        {"jev": {"api_key": "nope"}},
        enabled=True,
        decisions=("search_type",),
    )
    assert "api_key" not in config["jev"]
    assert config["jev"]["enabled"] is True
    assert config["jev"]["search_type"] is True
    assert config["jev"]["extract_quality"] is False
    assert config["jev"]["language_fill"] is False


def test_persist_key_file_is_mode_600(tmp_path):
    dest = tmp_path / "typesafe_api_key"
    path = persist_key_file("file-secret-value", dest)
    assert path == dest
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert path.read_text(encoding="utf-8").strip() == "file-secret-value"


def test_setup_dry_run_mentions_optional_jev(monkeypatch, capsys):
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(["setup", "--preset", "lean", "--dry-run"])
    monkeypatch.setattr(
        wsp.getpass,
        "getpass",
        lambda _prompt: (_ for _ in ()).throw(AssertionError("should not prompt")),
    )
    args.func(args)
    out = capsys.readouterr().out
    assert "Optional Jev" in out
    assert "ask whether to enable (default no)" in out
    assert "apikey_" not in out


def test_setup_jev_dry_run_shows_enable_plan(monkeypatch, capsys):
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(
        ["setup", "--preset", "lean", "--dry-run", "--jev", "--jev-key-file", "/tmp/does-not-need-to-exist"]
    )
    monkeypatch.setattr(
        wsp.getpass,
        "getpass",
        lambda _prompt: (_ for _ in ()).throw(AssertionError("should not prompt")),
    )
    args.func(args)
    out = capsys.readouterr().out
    assert "Plan: enable Jev" in out
    assert "search_type" in out
    assert "apikey_" not in out


def test_status_reports_jev_enabled_after_config_reload(tmp_path, capsys):
    config_path = tmp_path / "config.json"
    env_path = tmp_path / ".env"
    env_path.write_text("", encoding="utf-8")
    written = apply_jev_config(
        wsp._default_behavior_config(),
        enabled=True,
        decisions=JEV_DECISIONS,
    )
    wsp._write_behavior_config(config_path, written)
    on_disk = json.loads(config_path.read_text(encoding="utf-8"))
    assert on_disk["jev"]["enabled"] is True

    loaded = wsp._load_behavior_config(config_path)
    payload = wsp._status_payload({}, loaded)
    assert loaded.get("jev", {}).get("enabled") is True
    assert payload["jev"]["enabled"] is True
    assert payload["jev"]["decisions"]["search_type"] is True
    assert payload["jev"]["decisions"]["extract_quality"] is True
    assert payload["jev"]["decisions"]["language_fill"] is True

    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(
        ["status", "--plain", "--config-path", str(config_path), "--env-path", str(env_path)]
    )
    args.func(args)
    out = capsys.readouterr().out
    assert "Jev: on" in out


def test_apply_profile_effects_keeps_jev_block():
    from config import apply_profile_effects

    kept = apply_profile_effects(
        {
            "profile": "self_hosted",
            "jev": {"enabled": True, "search_type": True},
        }
    )
    assert kept["jev"]["enabled"] is True
    assert kept["jev"]["search_type"] is True


def test_setup_no_jev_dry_run_stays_disabled(monkeypatch, capsys):
    parser = wsp.argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(["setup", "--preset", "lean", "--dry-run", "--no-jev"])
    monkeypatch.setattr(
        wsp.getpass,
        "getpass",
        lambda _prompt: (_ for _ in ()).throw(AssertionError("should not prompt")),
    )
    args.func(args)
    out = capsys.readouterr().out
    assert "leave Jev disabled" in out
