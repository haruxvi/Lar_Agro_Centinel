"""Data access for the predios context."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.predios.models import Predio


class PredioRepository:
    """Reads and writes ``predios`` rows, always scoped by tenant."""

    def __init__(self, session: Session) -> None:
        """Bind the repository to a database session."""
        self._session = session

    def get_by_id(self, tenant_id: uuid.UUID, predio_id: uuid.UUID) -> Predio | None:
        """Return a tenant's predio by primary key, or ``None``."""
        statement = select(Predio).where(
            Predio.id == predio_id, Predio.tenant_id == tenant_id
        )
        return self._session.execute(statement).scalar_one_or_none()

    def list_for_tenant(self, tenant_id: uuid.UUID) -> Sequence[Predio]:
        """Return every predio owned by a tenant."""
        statement = select(Predio).where(Predio.tenant_id == tenant_id)
        return self._session.execute(statement).scalars().all()
