"""Rolling provider latency memory.

Every real provider call records its latency, result count and error flag in a
small rolling window (``provider_stats.json``). The hedged fallback reads the
latency quantile of the planned provider (``latency_quantile``) to decide when
a slow first provider is worth racing against the next one;
``get_provider_performance`` summarises the window. Routing does not read these
samples: the first provider comes from the query intent alone (routing.py).
"""

from __future__ import annotations

import json
import os
import statistics
import tempfile
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

try:  # POSIX only; Windows has no fcntl module.
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

from .cache import CACHE_DIR


PROVIDER_STATS_FILE = CACHE_DIR / "provider_stats.json"
# Rolling window: keep this many most-recent samples per provider.
MAX_SAMPLES_PER_PROVIDER = 50
# Ignore samples older than this; stale history should not set the hedge delay.
SAMPLE_MAX_AGE_SECONDS = 7 * 24 * 3600
# latency_quantile needs this many fresh successful samples before it answers.
MIN_SAMPLES_FOR_ADJUSTMENT = 5
# Latency at or above this earns no speed credit in the bench score (bench.py).
LATENCY_CEILING_SECONDS = 8.0

_STATS_LOCK = threading.Lock()


@contextmanager
def _stats_file_lock() -> Iterator[None]:
    """Serialize read-modify-write across processes (gateway, CLI, MCP).

    The thread lock only covers one process. Without a file lock, two
    processes that record at the same time overwrite each other's samples.
    """
    with _STATS_LOCK:
        if fcntl is None:
            yield
            return
        PROVIDER_STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
        lock_path = PROVIDER_STATS_FILE.with_name(PROVIDER_STATS_FILE.name + ".lock")
        with open(lock_path, "a", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def _load_stats() -> Dict[str, Any]:
    if not PROVIDER_STATS_FILE.exists():
        return {}
    try:
        with open(PROVIDER_STATS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, IOError):
        return {}


def _save_stats(state: Dict[str, Any]) -> None:
    PROVIDER_STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=PROVIDER_STATS_FILE.name + ".",
        suffix=".tmp",
        dir=str(PROVIDER_STATS_FILE.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp_name, PROVIDER_STATS_FILE)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def record_provider_outcome(
    provider: str,
    latency_seconds: float,
    result_count: int,
    error: bool,
    now: Optional[float] = None,
) -> None:
    """Append one provider-call outcome to the rolling window.

    Best-effort: stats must never break a search, so persistence errors are
    swallowed.
    """
    sample = {
        "t": int(now if now is not None else time.time()),
        "lat": round(max(0.0, float(latency_seconds)), 3),
        "n": int(max(0, result_count)),
        "err": bool(error),
    }
    try:
        with _stats_file_lock():
            state = _load_stats()
            samples = state.get(provider)
            if not isinstance(samples, list):
                samples = []
            samples.append(sample)
            state[provider] = samples[-MAX_SAMPLES_PER_PROVIDER:]
            _save_stats(state)
    except Exception:
        pass


def _fresh_samples(samples: Any, now: float) -> List[Dict[str, Any]]:
    if not isinstance(samples, list):
        return []
    cutoff = now - SAMPLE_MAX_AGE_SECONDS
    return [
        sample for sample in samples
        if isinstance(sample, dict) and int(sample.get("t", 0) or 0) >= cutoff
    ]


def get_provider_performance(provider: str, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Return summarized fresh performance for one provider, or None."""
    now_ts = now if now is not None else time.time()
    samples = _fresh_samples(_load_stats().get(provider), now_ts)
    if not samples:
        return None
    successes = [s for s in samples if not s.get("err")]
    empty = [s for s in successes if int(s.get("n", 0) or 0) == 0]
    latencies = [float(s.get("lat", 0.0) or 0.0) for s in successes]
    return {
        "samples": len(samples),
        "success_rate": round(len(successes) / len(samples), 3),
        "empty_rate": round(len(empty) / len(successes), 3) if successes else 0.0,
        "median_latency_seconds": round(statistics.median(latencies), 3) if latencies else None,
    }


def latency_quantile(provider: str, quantile: float = 0.75, now: Optional[float] = None) -> Optional[float]:
    """Latency (seconds) below which this share of recent successful calls finished.

    None until MIN_SAMPLES_FOR_ADJUSTMENT fresh successful samples exist.
    """
    now_ts = now if now is not None else time.time()
    latencies = sorted(
        float(sample.get("lat", 0.0) or 0.0)
        for sample in _fresh_samples(_load_stats().get(provider), now_ts)
        if not sample.get("err")
    )
    if len(latencies) < MIN_SAMPLES_FOR_ADJUSTMENT:
        return None
    index = min(len(latencies) - 1, max(0, int(round(quantile * (len(latencies) - 1)))))
    return latencies[index]
