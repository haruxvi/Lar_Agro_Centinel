"""Persistence models for the audit context."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Enum, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.db import Base, IPAddress
from app.shared.enums import AuditEventCategory, AuditEventSeverity, AuditOutcome
from app.shared.roles import Role


class AuditLog(Base):
    """A single append-only audit trail entry.

    Rows are never updated or deleted: the migration revokes UPDATE and DELETE
    on this table, and the service layer only exposes inserts.
    """

    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    event_category: Mapped[AuditEventCategory] = mapped_column(
        Enum(AuditEventCategory, native_enum=False, length=32)
    )
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[AuditEventSeverity] = mapped_column(
        Enum(AuditEventSeverity, native_enum=False, length=16)
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(index=True, default=None)
    actor_role: Mapped[Role | None] = mapped_column(
        Enum(Role, native_enum=False, length=32), default=None
    )
    actor_ip: Mapped[str | None] = mapped_column(IPAddress, default=None)
    actor_user_agent: Mapped[str | None] = mapped_column(String(512), default=None)
    target_resource_type: Mapped[str | None] = mapped_column(String(64), default=None)
    target_resource_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    predio_id: Mapped[uuid.UUID | None] = mapped_column(index=True, default=None)
    action: Mapped[str] = mapped_column(String(128))
    outcome: Mapped[AuditOutcome] = mapped_column(
        Enum(AuditOutcome, native_enum=False, length=16)
    )
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(index=True, default=None)
    # Hash chain is implemented in Phase 7; the column is reserved here.
    hash_prev: Mapped[str | None] = mapped_column(String(64), default=None)
