"""Persistence models for the predios context."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.db import Base


class Predio(Base):
    """A land parcel owned by a tenant."""

    __tablename__ = "predios"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(index=True)
    name: Mapped[str] = mapped_column(String(255))
    commune: Mapped[str | None] = mapped_column(String(255), default=None)
    area_hectares: Mapped[float | None] = mapped_column(Numeric(12, 4), default=None)
    geometry: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
