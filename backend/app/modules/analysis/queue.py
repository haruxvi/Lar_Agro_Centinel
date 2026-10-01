"""Hand analyses to the arq worker.

The API only enqueues; the worker (worker.py) runs. The queue is a protocol
so endpoint tests can stand in a fake and never touch arq or Sentinel Hub.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Protocol

from arq import create_pool
from arq.connections import RedisSettings
from redis.exceptions import RedisError

from app.modules.analysis.exceptions import QueueUnavailableError
from app.modules.analysis.worker import RUN_ANALYSIS, job_id_for
from app.shared.config import Settings, get_settings

# Fail fast: a request waiting on a dead Redis is worse than a clear 503.
_CONNECT_RETRIES = 1
_CONNECT_TIMEOUT_S = 2


class AnalysisQueue(Protocol):
    """Where a PENDING analysis goes to be run."""

    async def enqueue(self, analysis_id: uuid.UUID) -> None:
        """Queue the analysis; raise QueueUnavailableError if that fails."""
        ...


class ArqAnalysisQueue:
    """Enqueue on the arq queue the worker consumes.

    One connection per enqueue: analyses are rare (each one spends quota),
    so a pooled connection would sit idle almost all the time.
    """

    def __init__(self, settings: Settings) -> None:
        """Read the Redis address from the settings."""
        self._redis_settings = dataclasses.replace(
            RedisSettings.from_dsn(str(settings.redis_url)),
            conn_retries=_CONNECT_RETRIES,
            conn_timeout=_CONNECT_TIMEOUT_S,
        )

    async def enqueue(self, analysis_id: uuid.UUID) -> None:
        """Queue the job under a deterministic id: a second enqueue is a no-op."""
        try:
            pool = await create_pool(self._redis_settings)
        except (RedisError, OSError) as exc:
            raise QueueUnavailableError from exc
        try:
            await pool.enqueue_job(
                RUN_ANALYSIS, str(analysis_id), _job_id=job_id_for(analysis_id)
            )
        except (RedisError, OSError) as exc:
            raise QueueUnavailableError from exc
        finally:
            await pool.aclose()


def get_analysis_queue() -> AnalysisQueue:
    """Return the queue the API enqueues on (a FastAPI dependency)."""
    return ArqAnalysisQueue(get_settings())
