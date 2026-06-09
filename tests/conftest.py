import pytest

import provider_stats


@pytest.fixture(autouse=True)
def _isolate_provider_stats(tmp_path, monkeypatch):
    """Keep adaptive-routing stats out of the real cache dir during tests.

    Search tests record outcomes for mocked providers; without isolation those
    samples would pollute the operator's provider_stats.json and, worse, feed
    back into routing decisions and make routing tests order-dependent.
    """
    monkeypatch.setattr(provider_stats, "PROVIDER_STATS_FILE", tmp_path / "provider_stats.json")
