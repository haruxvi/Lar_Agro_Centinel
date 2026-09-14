"""Data access for the analysis context."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.analysis.models import AnalysisRun


class AnalysisRepository:
    """Reads and writes ``analysis_runs`` rows, always scoped by tenant."""

    def __init__(self, session: Session) -> None:
        """Bind the repository to a database session."""
        self._session = session

    def get_by_id(self, tenant_id: uuid.UUID, run_id: uuid.UUID) -> AnalysisRun | None:
        """Return a tenant's analysis run by primary key, or ``None``."""
        statement = select(AnalysisRun).where(
            AnalysisRun.id == run_id, AnalysisRun.tenant_id == tenant_id
        )
        return self._session.execute(statement).scalar_one_or_none()

    def list_for_predio(
        self, tenant_id: uuid.UUID, predio_id: uuid.UUID
    ) -> Sequence[AnalysisRun]:
        """Return every analysis run recorded for a predio."""
        statement = select(AnalysisRun).where(
            AnalysisRun.tenant_id == tenant_id, AnalysisRun.predio_id == predio_id
        )
        return self._session.execute(statement).scalars().all()
