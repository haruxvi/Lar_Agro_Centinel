"""The arq queue the API enqueues on, against the suite's Redis.

No worker consumes the suite's Redis index, so a queued job just sits there:
nothing runs, nothing reaches Sentinel Hub.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
import redis
from arq.jobs import Job, JobStatus

from app.modules.analysis.exceptions import QueueUnavailableError
from app.modules.analysis.queue import ArqAnalysisQueue
from app.modules.analysis.worker import RUN_ANALYSIS, job_id_for
from app.shared.config import get_settings


@pytest.fixture
def clean_queue(redis_client: redis.Redis) -> Iterator[redis.Redis]:
    redis_client.flushdb()
    yield redis_client
    redis_client.flushdb()


@pytest.mark.asyncio
async def test_an_analysis_is_queued_under_its_deterministic_job_id(
    clean_queue: redis.Redis,
) -> None:
    from arq import create_pool

    queue = ArqAnalysisQueue(get_settings())
    analysis_id = uuid.uuid4()

    await queue.enqueue(analysis_id)

    pool = await create_pool(queue._redis_settings)  # noqa: SLF001 - same target
    try:
        job = Job(job_id_for(analysis_id), pool)
        assert await job.status() is JobStatus.queued
        info = await job.info()
        assert info is not None
        assert info.function == RUN_ANALYSIS
        assert info.args == (str(analysis_id),)
    finally:
        await pool.aclose()


@pytest.mark.asyncio
async def test_queueing_the_same_analysis_twice_leaves_one_job(
    clean_queue: redis.Redis,
) -> None:
    queue = ArqAnalysisQueue(get_settings())
    analysis_id = uuid.uuid4()

    await queue.enqueue(analysis_id)
    await queue.enqueue(analysis_id)  # does not raise

    assert clean_queue.zcard("arq:queue") == 1


@pytest.mark.asyncio
async def test_an_unreachable_redis_is_a_queue_error_not_a_crash() -> None:
    unreachable = get_settings().model_copy(update={"redis_url": "redis://127.0.0.1:1/0"})

    with pytest.raises(QueueUnavailableError):
        await ArqAnalysisQueue(unreachable).enqueue(uuid.uuid4())
