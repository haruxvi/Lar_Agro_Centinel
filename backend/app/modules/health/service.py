"""Health reporting service.

Dependency probes (database, cache) are wired in a later step; for now the
service reports process liveness only.
"""

from __future__ import annotations

import time

from app.modules.health.schema import HealthReport
from app.shared.config import get_settings

_STARTED_AT = time.monotonic()


def uptime_seconds() -> int:
    """Return whole seconds elapsed since the process started."""
    return int(time.monotonic() - _STARTED_AT)


class HealthService:
    """Builds the health report exposed by ``GET /health``."""

    def report(self) -> HealthReport:
        """Return the current health report."""
        settings = get_settings()
        return HealthReport(
            status="healthy",
            checks={},
            version=settings.app_version,
            uptime_seconds=uptime_seconds(),
        )
