import json

import cache
import provider_health


def test_cache_put_writes_json_via_atomic_replace(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    replaced = []
    real_replace = cache.os.replace

    def tracking_replace(src, dst):
        replaced.append((src, dst))
        real_replace(src, dst)

    monkeypatch.setattr(cache.os, "replace", tracking_replace)

    cache.cache_put("query", "serper", 1, {"results": []})

    assert replaced
    assert replaced[0][1].parent == tmp_path
    assert replaced[0][1].exists()
    assert replaced[0][1].suffix == ".json"
    assert json.loads(replaced[0][1].read_text())["results"] == []


def test_provider_health_writes_json_via_atomic_replace(tmp_path, monkeypatch):
    health_path = tmp_path / "provider_health.json"
    monkeypatch.setattr(provider_health, "PROVIDER_HEALTH_FILE", health_path)
    replaced = []
    real_replace = provider_health.os.replace

    def tracking_replace(src, dst):
        replaced.append((src, dst))
        real_replace(src, dst)

    monkeypatch.setattr(provider_health.os, "replace", tracking_replace)

    provider_health.mark_provider_failure("serper", "boom")

    assert replaced
    assert replaced[0][1] == health_path
    assert json.loads(health_path.read_text())["serper"]["last_error"] == "boom"
