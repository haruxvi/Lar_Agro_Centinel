"""Public DTOs of the audit context."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class AuditLogRead(BaseModel):
    """Audit entry representation exposed to API clients."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    actor_id: uuid.UUID | None = None
    event: str
    resource_type: str
    resource_id: str | None = None
    request_id: str | None = None
    payload: dict[str, Any] | None = None
    created_at: datetime
