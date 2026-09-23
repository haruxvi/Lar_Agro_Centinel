"""Public DTOs of the meta context."""

from __future__ import annotations

from pydantic import BaseModel

from app.shared.roles import Role


class RoleMetaRead(BaseModel):
    """Role catalogue entry exposed to API clients."""

    role: Role
    label: str
    short_label: str
    color: str
    description: str
    requires_two_factor: bool


class RoleCatalogRead(BaseModel):
    """The full role catalogue."""

    roles: list[RoleMetaRead]
