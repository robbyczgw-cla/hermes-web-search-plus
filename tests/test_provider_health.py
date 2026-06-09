"""Cooldown escalation, failure decay, and rate-limit retry behavior."""

import time
from unittest import mock

import pytest

import provider_health
from http_client import ProviderRequestError


@pytest.fixture()
def health_file(tmp_path, monkeypatch):
    path = tmp_path / "provider_health.json"
    monkeypatch.setattr(provider_health, "PROVIDER_HEALTH_FILE", path)
    return path


def test_first_failure_uses_shortest_cooldown_step(health_file):
    info = provider_health.mark_provider_failure("serper", "boom")

    assert info["failure_count"] == 1
    assert info["cooldown_seconds"] == provider_health.COOLDOWN_STEPS_SECONDS[0]


def test_consecutive_failures_escalate_cooldown(health_file):
    provider_health.mark_provider_failure("serper", "boom")
    info = provider_health.mark_provider_failure("serper", "boom again")

    assert info["failure_count"] == 2
    assert info["cooldown_seconds"] == provider_health.COOLDOWN_STEPS_SECONDS[1]


def test_stale_failures_restart_escalation_ladder(health_file):
    provider_health.mark_provider_failure("serper", "boom")
    provider_health.mark_provider_failure("serper", "boom")

    stale = time.time() + provider_health.FAILURE_DECAY_SECONDS + 10
    with mock.patch.object(provider_health.time, "time", return_value=stale):
        info = provider_health.mark_provider_failure("serper", "much later")

    assert info["failure_count"] == 1
    assert info["cooldown_seconds"] == provider_health.COOLDOWN_STEPS_SECONDS[0]


def test_retry_after_raises_cooldown_floor_and_is_capped(health_file):
    info = provider_health.mark_provider_failure("serper", "rate limited", retry_after=120.0)
    assert info["cooldown_seconds"] == 120

    info = provider_health.mark_provider_failure("serper", "rate limited", retry_after=99999.0)
    assert info["cooldown_seconds"] == provider_health.COOLDOWN_STEPS_SECONDS[-1]


def test_retry_after_shorter_than_ladder_step_keeps_ladder_step(health_file):
    provider_health.mark_provider_failure("serper", "boom")
    info = provider_health.mark_provider_failure("serper", "rate limited", retry_after=5.0)

    assert info["cooldown_seconds"] == provider_health.COOLDOWN_STEPS_SECONDS[1]


def test_rate_limited_provider_retries_only_once(monkeypatch):
    sleeps = []
    monkeypatch.setattr(provider_health.time, "sleep", sleeps.append)
    attempts = []

    def operation():
        attempts.append(1)
        raise ProviderRequestError("rate limited", status_code=429, transient=True)

    with pytest.raises(ProviderRequestError):
        provider_health.execute_provider_with_retry("serper", operation, max_attempts=3)

    assert len(attempts) == provider_health.RATE_LIMIT_MAX_ATTEMPTS
    assert len(sleeps) == provider_health.RATE_LIMIT_MAX_ATTEMPTS - 1


def test_rate_limited_retry_honors_retry_after_delay(monkeypatch):
    sleeps = []
    monkeypatch.setattr(provider_health.time, "sleep", sleeps.append)
    attempts = []

    def operation():
        attempts.append(1)
        raise ProviderRequestError("rate limited", status_code=429, transient=True, retry_after=7.5)

    with pytest.raises(ProviderRequestError):
        provider_health.execute_provider_with_retry("serper", operation, max_attempts=3)

    assert sleeps == [7.5]
    assert len(attempts) == 2


def test_rate_limited_retry_skips_wait_longer_than_cap(monkeypatch):
    sleeps = []
    monkeypatch.setattr(provider_health.time, "sleep", sleeps.append)
    attempts = []

    def operation():
        attempts.append(1)
        raise ProviderRequestError(
            "rate limited",
            status_code=429,
            transient=True,
            retry_after=provider_health.MAX_RETRY_AFTER_WAIT_SECONDS + 1,
        )

    with pytest.raises(ProviderRequestError):
        provider_health.execute_provider_with_retry("serper", operation, max_attempts=3)

    assert sleeps == []
    assert len(attempts) == 1


def test_other_transient_errors_keep_full_retry_budget(monkeypatch):
    sleeps = []
    monkeypatch.setattr(provider_health.time, "sleep", sleeps.append)
    attempts = []

    def operation():
        attempts.append(1)
        raise ProviderRequestError("unavailable", status_code=503, transient=True)

    with pytest.raises(ProviderRequestError):
        provider_health.execute_provider_with_retry("serper", operation, max_attempts=3)

    assert len(attempts) == 3
    assert len(sleeps) == 2
