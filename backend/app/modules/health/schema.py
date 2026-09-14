"""Public DTOs of the health context."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

HealthStatus = Literal["healthy", "degraded", "unhealthy"]


class HealthReport(BaseModel):
    """Aggregated health of the service and its dependencies."""

    status: HealthStatus
    checks: dict[str, bool]
    version: str
    uptime_seconds: int
