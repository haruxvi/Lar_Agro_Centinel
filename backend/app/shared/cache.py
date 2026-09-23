"""Redis access shared across modules."""

from __future__ import annotations

from functools import lru_cache

import redis

from app.shared.config import get_settings


@lru_cache(maxsize=1)
def get_redis() -> redis.Redis:
    """Return the process-wide Redis client."""
    settings = get_settings()
    return redis.Redis.from_url(str(settings.redis_url), decode_responses=True)


def claim_once(key: str, ttl_seconds: int) -> bool:
    """Claim a single-use key.

    Returns True the first time the key is claimed and False for every replay
    within the TTL. Used to make one-time codes genuinely one-time: the check
    and the write are a single atomic ``SET NX``, so two concurrent requests
    cannot both win.
    """
    return bool(get_redis().set(key, "1", nx=True, ex=ttl_seconds))
