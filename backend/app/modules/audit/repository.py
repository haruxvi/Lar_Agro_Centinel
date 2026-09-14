"""Data access for the audit context."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.audit.models import AuditLog


class AuditRepository:
    """Reads and appends ``audit_log`` rows, always scoped by tenant."""

    def __init__(self, session: Session) -> None:
        """Bind the repository to a database session."""
        self._session = session

    def get_by_id(self, tenant_id: uuid.UUID, entry_id: uuid.UUID) -> AuditLog | None:
        """Return a tenant's audit entry by primary key, or ``None``."""
        statement = select(AuditLog).where(
            AuditLog.id == entry_id, AuditLog.tenant_id == tenant_id
        )
        return self._session.execute(statement).scalar_one_or_none()

    def list_for_tenant(
        self, tenant_id: uuid.UUID, limit: int = 100
    ) -> Sequence[AuditLog]:
        """Return the most recent audit entries of a tenant."""
        statement = (
            select(AuditLog)
            .where(AuditLog.tenant_id == tenant_id)
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
        )
        return self._session.execute(statement).scalars().all()
