"""Public DTOs of the predios context."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class PredioRead(BaseModel):
    """Predio representation exposed to API clients."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    commune: str | None = None
    area_hectares: float | None = None
    geometry: dict[str, Any] | None = None
    created_at: datetime
