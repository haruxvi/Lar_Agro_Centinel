"""Public DTOs of the auth context."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr

from app.shared.roles import Role


class UserRead(BaseModel):
    """User representation exposed to API clients."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    email: EmailStr
    full_name: str
    role: Role
    is_active: bool
    created_at: datetime


class TokenPair(BaseModel):
    """Access token issued after a successful authentication."""

    access_token: str
    token_type: str = "bearer"  # noqa: S105 - OAuth2 response field, not a secret
    expires_in: int
