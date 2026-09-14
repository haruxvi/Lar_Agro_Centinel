"""Public DTOs of the analysis context."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class AnalysisRunRead(BaseModel):
    """Analysis run representation exposed to API clients."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    predio_id: uuid.UUID
    kind: str
    observed_on: date
    result: dict[str, Any] | None = None
    created_at: datetime
