"""arq worker: runs analyses outside the HTTP request.

Start with::

    arq app.modules.analysis.worker.WorkerSettings

(``PYTHONPATH=backend`` from the repository root, or the ``worker`` service in
docker-compose.yml.) A Sentinel Hub round trip takes 10-60 s; blocking an
HTTP worker that long would stall the API with a handful of users.
"""

from __future__ import annotations

import uuid
from typing import Any, ClassVar, Final

from arq import cron
from arq.connections import RedisSettings
from sqlalchemy.orm import Session, sessionmaker

from app.modules.analysis.availability import analysis_available
from app.modules.analysis.exceptions import AnalysisUnavailableError
from app.modules.analysis.sentinel_client import SentinelClient
from app.modules.analysis.service import AnalysisService
from app.modules.audit import events
from app.modules.audit.service import AuditService
from app.shared.config import Settings, get_settings
from app.shared.db import get_session_factory
from app.shared.enums import AuditOutcome
from app.shared.storage import build_storage

RUN_ANALYSIS: Final = "run_analysis"

# Each job holds rasters in memory: a few in parallel can exhaust the RAM of
# a small container. Raise only after measuring.
MAX_CONCURRENT_JOBS: Final = 2


def job_id_for(analysis_id: uuid.UUID) -> str:
    """Return the arq job id of an analysis: enqueueing twice is a no-op."""
    return f"analysis:{analysis_id}"


def _audit_sink(session_factory: sessionmaker[Session]) -> Any:
    """Record the Sentinel client's system events in the audit trail."""

    async def record(event_type: str, details: dict[str, object]) -> None:
        spec = events.ANALYSIS_EVENTS[event_type]
        with session_factory() as session:
            AuditService(session).record(
                event_category=spec.category,
                event_type=event_type,
                severity=spec.severity,
                action=event_type.lower().replace("_", " "),
                outcome=AuditOutcome.FAILURE,
                details=details,
            )
            session.commit()

    return record


async def startup(ctx: dict[str, Any]) -> None:
    """Build what every job shares: settings, storage, database sessions."""
    settings = get_settings()
    ctx["settings"] = settings
    ctx["storage"] = build_storage(settings)
    ctx["session_factory"] = get_session_factory()


async def run_analysis(ctx: dict[str, Any], analysis_id: str) -> str | None:
    """Run one analysis. Idempotent: a retry on a finished analysis does nothing."""
    settings: Settings = ctx["settings"]
    session_factory: sessionmaker[Session] = ctx["session_factory"]
    identifier = uuid.UUID(analysis_id)
    with session_factory() as session:
        service = AnalysisService(session, settings)
        if not analysis_available(settings):
            service.fail_pending(
                identifier, "ANALYSIS_UNAVAILABLE", AnalysisUnavailableError().as_dict()
            )
            return None
        client = SentinelClient(settings, ctx["redis"], _audit_sink(session_factory))
        try:
            status = await service.run(identifier, client, ctx["storage"])
        finally:
            await client.aclose()
    return status.value if status else None


async def expire_stale_analyses(ctx: dict[str, Any]) -> int:
    """Fail analyses stuck in RUNNING past the job timeout (dead workers)."""
    session_factory: sessionmaker[Session] = ctx["session_factory"]
    with session_factory() as session:
        return AnalysisService(session, ctx["settings"]).expire_stale()


class WorkerSettings:
    """arq configuration, read by ``arq app.modules.analysis.worker.WorkerSettings``."""

    functions: ClassVar[list[Any]] = [run_analysis]
    cron_jobs: ClassVar[list[Any]] = [
        cron(expire_stale_analyses, minute=set(range(0, 60, 5)), run_at_startup=True)
    ]
    on_startup = startup
    max_jobs = MAX_CONCURRENT_JOBS
    # A little over the analysis timeout, so the sweeper is the one that
    # records a TIMEOUT rather than arq killing the job silently.
    job_timeout = get_settings().analysis_job_timeout_s + 60
    redis_settings = RedisSettings.from_dsn(str(get_settings().redis_url))
