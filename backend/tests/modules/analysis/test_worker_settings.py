"""The arq worker survives short Redis outages and can be health-checked.

With arq's defaults the worker retried Redis only while starting; once
running, one failed command (Redis restarting, a few seconds without network)
ended the process. Found when a laptop went to sleep on 2026-11-20.
"""

from __future__ import annotations

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.modules.analysis.worker import (
    REDIS_BACKOFF_CAP_S,
    WorkerSettings,
    worker_redis_settings,
)
from app.shared.config import Settings, get_settings


def _settings(**overrides: object) -> Settings:
    return get_settings().model_copy(update=overrides)


def test_every_redis_command_is_retried_on_connection_errors_and_timeouts() -> None:
    redis = worker_redis_settings(_settings(worker_redis_command_retries=7))

    assert redis.retry_on_timeout is True
    assert set(redis.retry_on_error or []) == {RedisConnectionError, RedisTimeoutError}
    assert redis.retry is not None
    assert redis.retry._retries == 7  # noqa: SLF001 - no public accessor
    # The backoff is capped: a long outage is retried every few seconds, not
    # at ever longer intervals.
    assert redis.retry._backoff.compute(20) == REDIS_BACKOFF_CAP_S  # noqa: SLF001


def test_startup_waits_for_redis_to_come_up() -> None:
    redis = worker_redis_settings(
        _settings(
            worker_redis_conn_timeout_s=5,
            worker_redis_conn_retries=30,
            worker_redis_conn_retry_delay_s=2,
        )
    )
    assert (redis.conn_timeout, redis.conn_retries, redis.conn_retry_delay) == (5, 30, 2)


def test_the_connection_still_points_at_the_configured_redis() -> None:
    settings = _settings(redis_url="redis://cache.internal:6380/4")
    redis = worker_redis_settings(settings)
    assert (redis.host, redis.port, redis.database) == ("cache.internal", 6380, 4)


def test_the_worker_uses_the_resilient_settings_and_a_frequent_health_key() -> None:
    settings = get_settings()
    assert WorkerSettings.redis_settings.retry_on_timeout is True
    assert WorkerSettings.redis_settings.retry is not None
    # The container health check (arq --check) reads this key: it must be
    # refreshed often enough to notice a hung worker within a minute or two.
    assert WorkerSettings.health_check_interval == settings.worker_health_check_interval_s
    assert WorkerSettings.health_check_interval <= 60
