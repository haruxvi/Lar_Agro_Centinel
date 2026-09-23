"""HTTP surface of the meta context."""

from __future__ import annotations

from fastapi import APIRouter

from app.modules.meta.schema import RoleCatalogRead, RoleMetaRead
from app.shared.roles import ROLE_META, ROLES_2FA_REQUIRED, Role

router = APIRouter(prefix="/meta", tags=["meta"])


@router.get("/roles", response_model=RoleCatalogRead)
def read_roles() -> RoleCatalogRead:
    """Return the role catalogue.

    Public on purpose: it carries no tenant data, and lets clients that do not
    go through this frontend's build (a mobile app, for instance) read the same
    catalogue the backend uses.
    """
    return RoleCatalogRead(
        roles=[
            RoleMetaRead(
                role=role,
                label=meta.label,
                short_label=meta.short_label,
                color=meta.color,
                description=meta.description,
                requires_two_factor=role in ROLES_2FA_REQUIRED,
            )
            for role, meta in ((role, ROLE_META[role]) for role in Role)
        ]
    )
