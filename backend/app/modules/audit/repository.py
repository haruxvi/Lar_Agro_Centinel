"""Data access for the audit context."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.audit.models import AuditLog


class AuditRepository:
    """Appends and reads ``audit_log`` rows.

    Append-only by design: no update or delete methods exist here, and the
    database revokes those privileges as a second line of defence.
    """

    def __init__(self, session: Session) -> None:
        """Bind the repository to a database session."""
        self._session = session

    def append(self, entry: AuditLog) -> AuditLog:
        """Insert a new audit entry and flush it."""
        self._session.add(entry)
        self._session.flush()
        return entry

    def get_by_id(self, entry_id: uuid.UUID) -> AuditLog | None:
        """Return an audit entry by primary key, or ``None``."""
        return self._session.get(AuditLog, entry_id)

    def list_for_actor(
        self, actor_user_id: uuid.UUID, limit: int = 100
    ) -> Sequence[AuditLog]:
        """Return the most recent audit entries recorded for one actor."""
        statement = (
            select(AuditLog)
            .where(AuditLog.actor_user_id == actor_user_id)
            .order_by(AuditLog.timestamp.desc())
            .limit(limit)
        )
        return self._session.execute(statement).scalars().all()

    def list_for_predio(
        self, predio_id: uuid.UUID, limit: int = 100
    ) -> Sequence[AuditLog]:
        """Return the most recent audit entries recorded for one predio."""
        statement = (
            select(AuditLog)
            .where(AuditLog.predio_id == predio_id)
            .order_by(AuditLog.timestamp.desc())
            .limit(limit)
        )
        return self._session.execute(statement).scalars().all()
