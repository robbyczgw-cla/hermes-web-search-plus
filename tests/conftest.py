import pytest

import bench
import provider_stats


@pytest.fixture(autouse=True)
def _isolate_provider_stats(tmp_path, monkeypatch):
    """Keep adaptive-routing stats out of the real cache dir during tests.

    Search tests record outcomes for mocked providers; without isolation those
    samples would pollute the operator's provider_stats.json and, worse, feed
    back into routing decisions and make routing tests order-dependent.
    Bench history is isolated for the same reason: bench tests run mocked
    benches whose records must not land in the operator's real history file.
    """
    monkeypatch.setattr(provider_stats, "PROVIDER_STATS_FILE", tmp_path / "provider_stats.json")
    monkeypatch.setattr(bench, "BENCH_HISTORY_FILE", tmp_path / "bench_history.jsonl")
