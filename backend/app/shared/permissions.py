"""RBAC and ABAC: what each role may do, and on which predio.

Two layers answer two different questions. The role map (RBAC) answers "may
this kind of user do this at all?", and the predio grants (ABAC) answer "may
they do it *here*?". Both must say yes.

The map encodes segregation of duties on purpose: whoever plans an operation
does not execute it, and whoever recommends a treatment does not approve it.
The invariants that guard those rules are enforced by tests.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from app.shared.roles import Role


class Permission(StrEnum):
    """A single capability that can be granted to a role."""

    # Predios
    PREDIO_VIEW = "predio:view"
    PREDIO_CREATE = "predio:create"
    PREDIO_UPDATE = "predio:update"
    PREDIO_DELETE = "predio:delete"
    PREDIO_ASSIGN_USERS = "predio:assign_users"
    # Operations
    MISSION_PLAN = "mission:plan"
    MISSION_EXECUTE = "mission:execute"
    APPLICATION_APPROVE = "application:approve"
    APPLICATION_EXECUTE = "application:execute"
    # Analysis
    ANALYSIS_VIEW = "analysis:view"
    ANALYSIS_REPORT = "analysis:report"
    # Captures
    CAPTURE_UPLOAD = "capture:upload"
    CAPTURE_VIEW = "capture:view"
    # Warehouse
    INVENTORY_VIEW = "inventory:view"
    INVENTORY_MOVE_IN = "inventory:move_in"
    INVENTORY_MOVE_OUT = "inventory:move_out"
    INVENTORY_MOVE_RESTRICTED = "inventory:move_restricted"
    INVENTORY_APPROVE = "inventory:approve"
    # Users
    USER_VIEW = "user:view"
    USER_MANAGE_ROLES = "user:manage_roles"
    # Configuration
    RESPONSIBLE_MODE_CONFIGURE = "responsible_mode:configure"
    # Audit
    AUDIT_VIEW_OWN = "audit:view_own"
    AUDIT_VIEW_PREDIO = "audit:view_predio"
    AUDIT_VIEW_ALL = "audit:view_all"
    AUDIT_EXPORT = "audit:export"


_READ_ONLY_PERMISSIONS: frozenset[Permission] = frozenset(
    {
        Permission.PREDIO_VIEW,
        Permission.ANALYSIS_VIEW,
        Permission.CAPTURE_VIEW,
        Permission.INVENTORY_VIEW,
        Permission.USER_VIEW,
    }
)

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    # Governance: sees everything on their own predios and decides who works
    # there, but never operates equipment and cannot export the audit trail.
    Role.PROPIETARIO: frozenset(
        {
            Permission.PREDIO_VIEW,
            Permission.PREDIO_CREATE,
            Permission.PREDIO_UPDATE,
            Permission.PREDIO_DELETE,
            Permission.PREDIO_ASSIGN_USERS,
            Permission.ANALYSIS_VIEW,
            Permission.CAPTURE_VIEW,
            Permission.INVENTORY_VIEW,
            Permission.USER_VIEW,
            Permission.USER_MANAGE_ROLES,
            Permission.RESPONSIBLE_MODE_CONFIGURE,
            Permission.AUDIT_VIEW_OWN,
            Permission.AUDIT_VIEW_PREDIO,
        }
    ),
    # Plans and approves; never executes, never touches roles.
    Role.ADMIN_OPERACIONES: frozenset(
        {
            Permission.PREDIO_VIEW,
            Permission.MISSION_PLAN,
            Permission.APPLICATION_APPROVE,
            Permission.ANALYSIS_VIEW,
            Permission.CAPTURE_VIEW,
            Permission.INVENTORY_VIEW,
            Permission.USER_VIEW,
            Permission.AUDIT_VIEW_OWN,
        }
    ),
    # Analyses and recommends; does not execute and does not approve.
    Role.AGRONOMO: frozenset(
        {
            Permission.PREDIO_VIEW,
            Permission.ANALYSIS_VIEW,
            Permission.ANALYSIS_REPORT,
            Permission.CAPTURE_VIEW,
            Permission.AUDIT_VIEW_OWN,
        }
    ),
    # Flies the missions someone else planned.
    Role.OPERADOR_DRONE: frozenset(
        {
            Permission.PREDIO_VIEW,
            Permission.MISSION_EXECUTE,
            Permission.CAPTURE_UPLOAD,
            Permission.CAPTURE_VIEW,
            Permission.AUDIT_VIEW_OWN,
        }
    ),
    # Applies what someone else approved.
    Role.APLICADOR: frozenset(
        {
            Permission.PREDIO_VIEW,
            Permission.APPLICATION_EXECUTE,
            Permission.INVENTORY_VIEW,
            Permission.AUDIT_VIEW_OWN,
        }
    ),
    # Ordinary stock movements only.
    Role.BODEGUERO: frozenset(
        {
            Permission.INVENTORY_VIEW,
            Permission.INVENTORY_MOVE_IN,
            Permission.INVENTORY_MOVE_OUT,
            Permission.AUDIT_VIEW_OWN,
        }
    ),
    # Everything the bodeguero does, plus restricted movements and approvals.
    Role.JEFE_BODEGA: frozenset(
        {
            Permission.INVENTORY_VIEW,
            Permission.INVENTORY_MOVE_IN,
            Permission.INVENTORY_MOVE_OUT,
            Permission.INVENTORY_MOVE_RESTRICTED,
            Permission.INVENTORY_APPROVE,
            Permission.AUDIT_VIEW_OWN,
            Permission.AUDIT_VIEW_PREDIO,
        }
    ),
    # Reads everything and exports it. Writes nothing, ever.
    Role.AUDITOR: frozenset(
        _READ_ONLY_PERMISSIONS
        | {
            Permission.AUDIT_VIEW_OWN,
            Permission.AUDIT_VIEW_PREDIO,
            Permission.AUDIT_VIEW_ALL,
            Permission.AUDIT_EXPORT,
        }
    ),
    # External party: only their own notifications, which are not predio data.
    # Their permissions arrive with the notifications module.
    Role.APICULTOR: frozenset(),
}


@runtime_checkable
class PredioGrant(Protocol):
    """A role granted to a user on one predio.

    Structural on purpose: ``shared`` must not import a module's models.
    ``UserPredioRole`` satisfies this shape.
    """

    predio_id: uuid.UUID
    role: Role
    expires_at: datetime | None


def _is_active(grant: PredioGrant, now: datetime) -> bool:
    return grant.expires_at is None or grant.expires_at > now


def permissions_for(roles: Iterable[Role]) -> frozenset[Permission]:
    """Return the union of permissions held by ``roles``."""
    granted: set[Permission] = set()
    for role in roles:
        granted |= ROLE_PERMISSIONS.get(role, frozenset())
    return frozenset(granted)


def has_permission(roles: Iterable[Role], permission: Permission) -> bool:
    """Return whether any of ``roles`` grants ``permission``."""
    return permission in permissions_for(roles)


def roles_on_predio(
    predio_grants: Sequence[PredioGrant],
    predio_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> frozenset[Role]:
    """Return the non-expired roles a user holds on one predio."""
    moment = now or datetime.now(UTC)
    return frozenset(
        grant.role
        for grant in predio_grants
        if grant.predio_id == predio_id and _is_active(grant, moment)
    )


def can_access_predio(
    predio_grants: Sequence[PredioGrant],
    predio_id: uuid.UUID,
    permission: Permission,
    *,
    global_roles: Iterable[Role] = (),
    now: datetime | None = None,
) -> bool:
    """Return whether the user may exercise ``permission`` on ``predio_id``.

    Fail-closed: a user with no grant on that predio is denied, which is what
    stops one tenant from reaching another's data by changing an id in the URL.
    A global role (the auditor's read-only access, for instance) applies
    everywhere and is checked separately.
    """
    if has_permission(global_roles, permission):
        return True
    return has_permission(roles_on_predio(predio_grants, predio_id, now=now), permission)


def accessible_predio_ids(
    predio_grants: Sequence[PredioGrant],
    *,
    now: datetime | None = None,
) -> frozenset[uuid.UUID]:
    """Return every predio the user currently holds a live grant on."""
    moment = now or datetime.now(UTC)
    return frozenset(
        grant.predio_id for grant in predio_grants if _is_active(grant, moment)
    )
