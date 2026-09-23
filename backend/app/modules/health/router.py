"""HTTP surface of the health context."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from app.modules.health.schema import HealthReport
from app.modules.health.service import HealthService
from app.shared.db import get_session

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthReport)
def read_health(
    response: Response, session: Annotated[Session, Depends(get_session)]
) -> HealthReport:
    """Report whether the service and its dependencies are usable.

    Returns 503 only when the service cannot do its job at all; a degraded
    dependency still answers 200 so load balancers keep routing traffic.
    """
    report = HealthService(session).report()
    if report.status == "unhealthy":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return report
