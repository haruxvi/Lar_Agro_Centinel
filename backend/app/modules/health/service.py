"""Health reporting.

Checks the dependencies the API cannot work without, and says nothing about
how they are built: no library versions, no hostnames, no error details. A
health endpoint is reachable by anyone who can reach the service.
"""

from __future__ import annotations

import time

import redis
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.modules.health.schema import HealthReport, HealthStatus
from app.shared.cache import get_redis
from app.shared.config import get_settings
from app.shared.logging import get_logger

logger = get_logger(__name__)

_STARTED_AT = time.monotonic()


def uptime_seconds() -> int:
    """Return whole seconds elapsed since the process started."""
    return int(time.monotonic() - _STARTED_AT)


class HealthService:
    """Builds the health report exposed by ``GET /health``."""

    def __init__(self, session: Session) -> None:
        """Bind the service to a database session."""
        self._session = session

    def check_database(self) -> bool:
        """Return whether the database answers a trivial query."""
        try:
            self._session.execute(text("SELECT 1"))
        except SQLAlchemyError:
            logger.warning("health_check_failed", dependency="database")
            return False
        return True

    def check_cache(self) -> bool:
        """Return whether Redis answers a ping."""
        try:
            return bool(get_redis().ping())
        except redis.RedisError:
            logger.warning("health_check_failed", dependency="redis")
            return False

    def report(self) -> HealthReport:
        """Return the current health report.

        The database is core: without it nothing works, so losing it is
        unhealthy. Redis backs caching and rate limiting, which degrade the
        service rather than stop it.
        """
        checks = {"database": self.check_database(), "redis": self.check_cache()}

        status: HealthStatus
        if not checks["database"]:
            status = "unhealthy"
        elif not checks["redis"]:
            status = "degraded"
        else:
            status = "healthy"

        return HealthReport(
            status=status,
            checks=checks,
            version=get_settings().app_version,
            uptime_seconds=uptime_seconds(),
        )
