"""Public DTOs of the audit context."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.shared.enums import AuditEventCategory, AuditEventSeverity, AuditOutcome
from app.shared.roles import Role


class AuditLogRead(BaseModel):
    """Audit entry representation exposed to API clients."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    timestamp: datetime
    event_category: AuditEventCategory
    event_type: str
    severity: AuditEventSeverity
    actor_user_id: uuid.UUID | None = None
    actor_role: Role | None = None
    actor_ip: str | None = None
    target_resource_type: str | None = None
    target_resource_id: uuid.UUID | None = None
    predio_id: uuid.UUID | None = None
    action: str
    outcome: AuditOutcome
    details: dict[str, Any] = {}
    correlation_id: uuid.UUID | None = None
