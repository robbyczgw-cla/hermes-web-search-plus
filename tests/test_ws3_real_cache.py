"""Task-7 copied-real-cache and no-side-effect acceptance regression."""

from __future__ import annotations

import hashlib
import http.client
import json
import sqlite3
import threading
from pathlib import Path

import extract_bench_v3
import operator_console_v3 as console
import ui


TOKEN = "task-7-real-cache-token"


def fingerprint(root: Path) -> dict[str, tuple[int, str]]:
    return {
        path.relative_to(root).as_posix(): (
            path.stat().st_size,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def build_real_shaped_cache(root: Path) -> Path:
    (root / "provider_health.json").write_text(
        json.dumps({"linkup": {"cooldown_until": 0, "failure_count": 0}}),
        encoding="utf-8",
    )
    (root / "provider_stats.json").write_text(
        json.dumps({"linkup": {"success": 12, "failure": 1}}), encoding="utf-8"
    )
    (root / "usage_events.json").write_text("[]\n", encoding="utf-8")
    (root / "legacy-cache.json").write_text(
        json.dumps({"query": "must never reach the console"}), encoding="utf-8"
    )

    response = root / "v3" / "response" / "extract"
    response.mkdir(parents=True)
    (response / "owned.json").write_text(
        json.dumps(
            {
                "owner": "web-search-plus:v3",
                "created_at": 1_783_890_000.0,
                "payload": {"content": "must never reach the console"},
            }
        ),
        encoding="utf-8",
    )
    (response / "foreign.json").write_text(
        json.dumps({"owner": "someone-else", "created_at": 1_783_890_001.0}),
        encoding="utf-8",
    )
    (response / "corrupt.json").write_text("{not-json", encoding="utf-8")

    fulltext = root / "web" / "v3"
    fulltext.mkdir(parents=True)
    (fulltext / "owned.md").write_text(
        '<!-- wsp:web_text_v3 {"version":1} -->\nprivate source text\n',
        encoding="utf-8",
    )
    (fulltext / "foreign.md").write_text("# foreign\n", encoding="utf-8")
    (fulltext / "corrupt.md").write_bytes(b"\xff\xfe")

    state_path = root / "state" / "v3.sqlite3"
    state_path.parent.mkdir(parents=True)
    with sqlite3.connect(state_path) as connection:
        connection.execute("CREATE TABLE circuit_state (state TEXT NOT NULL)")
        connection.executemany(
            "INSERT INTO circuit_state(state) VALUES (?)",
            [("closed",), ("open",), ("half_open",), ("blocked_auth",)],
        )

    record = {
        "schema_version": 1,
        "kind": "extract",
        "timestamp": 1_783_890_100.0,
        "ok": True,
        "providers": [
            {
                "provider": "linkup",
                "score": 0.9,
                "success_rate": 1.0,
                "median_latency_seconds": 0.2,
                "error_count": 0,
            }
        ],
        "recommended_priority": ["linkup"],
    }
    assert extract_bench_v3.BenchmarkHistoryJournal(root).append(record) is True
    return state_path


def get_json(server, path: str) -> tuple[int, dict]:
    connection = http.client.HTTPConnection(
        "127.0.0.1", server.server_port, timeout=3
    )
    connection.request(
        "GET",
        path,
        headers={
            "Host": f"127.0.0.1:{server.server_port}",
            "Authorization": f"Bearer {TOKEN}",
        },
    )
    response = connection.getresponse()
    payload = json.loads(response.read())
    status = response.status
    connection.close()
    return status, payload


def test_copied_real_cache_is_truthful_and_http_rendering_has_no_side_effects(
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / "copied-real-cache"
    cache_root.mkdir()
    state_path = build_real_shaped_cache(cache_root)
    before = fingerprint(cache_root)

    overview = console.build_overview(
        cache_root=cache_root,
        config={},
        provider_ids=["linkup", "tavily"],
        state_path=state_path,
        plugin_version="3.0.0-dev",
        now=lambda: 1_783_890_200.0,
    )
    history = console.build_benchmark_history(cache_root=cache_root)

    assert overview["cache"]["response_entries"] == 1
    assert overview["cache"]["full_text_entries"] == 1
    assert overview["circuits"] == {
        "closed": 1,
        "open": 2,
        "blocked_auth": 1,
        "blocked_quota": 0,
        "unknown": 0,
    }
    assert overview["engine"]["state_available"] is True
    assert overview["benchmark_summary"]["extract_collected"] is True
    assert history["availability"]["extract"] == "collected"
    assert history["availability"]["search"] == "not_collected"

    serialized = console.serialize_endpoint_payload(overview) + console.serialize_endpoint_payload(
        history
    )
    for forbidden in (
        b"must never reach the console",
        b"private source text",
        b"legacy-cache.json",
        str(cache_root).encode(),
    ):
        assert forbidden not in serialized

    server = ui.create_server(
        host="127.0.0.1",
        port=0,
        token=TOKEN,
        cache_root=cache_root,
        state_path=state_path,
        config={},
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for path in (
            "/api/v3/overview",
            "/api/v3/receipts?limit=100",
            "/api/v3/benchmark-history?limit=100",
        ):
            status, payload = get_json(server, path)
            assert status == 200
            assert payload["schema_version"] == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert fingerprint(cache_root) == before
