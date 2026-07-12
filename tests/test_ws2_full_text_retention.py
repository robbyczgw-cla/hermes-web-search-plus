from __future__ import annotations

import os
from pathlib import Path

import bounded_context_v3
from bounded_context_v3 import FullTextStore


def test_full_text_ttl_evicts_only_expired_owned_entries(tmp_path: Path) -> None:
    now = 2_000_000.0
    store = FullTextStore(tmp_path, ttl_seconds=100, max_bytes=1_000_000, now=lambda: now)
    old = store.store("https://old.example/doc", "old text")
    fresh = store.store("https://fresh.example/doc", "fresh text")
    old_path = store.path_for_key(old["reference"]["key"])
    fresh_path = store.path_for_key(fresh["reference"]["key"])
    os.utime(old_path, (now - 101, now - 101))
    os.utime(fresh_path, (now - 10, now - 10))

    stats = store.enforce_retention()

    assert stats["ttl_evicted"] == 1
    assert not old_path.exists()
    assert fresh_path.exists()


def test_size_pressure_evicts_oldest_mtime_first(tmp_path: Path) -> None:
    now = 2_000_000.0
    roomy = FullTextStore(tmp_path, ttl_seconds=10_000, max_bytes=1_000_000, now=lambda: now)
    first = roomy.store("https://one.example/doc", "A" * 500)
    second = roomy.store("https://two.example/doc", "B" * 500)
    third = roomy.store("https://three.example/doc", "C" * 500)
    paths = [roomy.path_for_key(item["reference"]["key"]) for item in (first, second, third)]
    for index, path in enumerate(paths):
        os.utime(path, (now - 30 + index * 10, now - 30 + index * 10))
    keep_two = paths[1].stat().st_size + paths[2].stat().st_size
    bounded = FullTextStore(tmp_path, ttl_seconds=10_000, max_bytes=keep_two, now=lambda: now)

    stats = bounded.enforce_retention()

    assert stats["size_evicted"] == 1
    assert not paths[0].exists()
    assert paths[1].exists() and paths[2].exists()


def test_retention_preserves_foreign_and_other_wsp_state(tmp_path: Path) -> None:
    store = FullTextStore(tmp_path, ttl_seconds=0, max_bytes=0, now=lambda: 2_000_000.0)
    foreign = {
        tmp_path / "provider_stats.json": b"stats",
        tmp_path / "provider_health.json": b"health",
        tmp_path / "state.sqlite3": b"sqlite",
        tmp_path / "evidence.json": b"evidence",
        tmp_path / "web" / "foreign.md": b"foreign markdown",
        tmp_path / "web" / "binary.bin": b"binary",
    }
    for path, content in foreign.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    store.enforce_retention()

    assert {path: path.read_bytes() for path in foreign} == foreign


def test_disappearing_file_during_retention_does_not_crash(tmp_path: Path, monkeypatch) -> None:
    store = FullTextStore(tmp_path, now=lambda: 2_000_000.0)
    stored = store.store("https://gone.example/doc", "text")
    path = store.path_for_key(stored["reference"]["key"])
    path.unlink()
    monkeypatch.setattr(store, "_owned_files", lambda: [path])

    stats = store.enforce_retention()

    assert stats["errors"] == 0


def test_unwritable_store_degrades_without_fake_reference(tmp_path: Path, monkeypatch) -> None:
    store = FullTextStore(tmp_path)

    def deny(*_args, **_kwargs):
        raise PermissionError("read only")

    monkeypatch.setattr(bounded_context_v3, "_atomic_write_owned", deny)
    result = store.store("https://denied.example/doc", "full text")

    assert result == {
        "storage_attempted": True,
        "storage_succeeded": False,
        "reference": None,
        "full_text_sha256": None,
        "full_text_chars": None,
    }


def test_owned_orphan_temp_is_cleaned_but_foreign_temp_is_preserved(tmp_path: Path) -> None:
    web = tmp_path / "web"
    web.mkdir(parents=True)
    owned = web / ".wsp-v3-orphan.tmp"
    foreign = web / "someone-else.tmp"
    owned.write_text("owned", encoding="utf-8")
    foreign.write_text("foreign", encoding="utf-8")
    store = FullTextStore(tmp_path)

    stats = store.cleanup_orphans()

    assert stats["orphan_temps_removed"] == 1
    assert not owned.exists()
    assert foreign.read_text(encoding="utf-8") == "foreign"


def test_lookup_enforces_ttl_and_returns_full_text(tmp_path: Path) -> None:
    now = 2_000_000.0
    store = FullTextStore(tmp_path, ttl_seconds=100, now=lambda: now)
    stored = store.store("https://lookup.example/doc", "# Full\nText")
    key = stored["reference"]["key"]

    assert store.lookup(key) == "# Full\nText"

    path = store.path_for_key(key)
    os.utime(path, (now - 101, now - 101))
    assert store.lookup(key) is None
    assert not path.exists()
