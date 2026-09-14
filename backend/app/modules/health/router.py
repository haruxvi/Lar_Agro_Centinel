"""HTTP surface of the health context."""

from __future__ import annotations

from fastapi import APIRouter

from app.modules.health.schema import HealthReport
from app.modules.health.service import HealthService

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthReport)
def read_health() -> HealthReport:
    """Report whether the service and its dependencies are usable."""
    return HealthService().report()
