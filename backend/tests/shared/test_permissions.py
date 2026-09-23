"""RBAC role map, its invariants, and ABAC predio scoping."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from app.shared.permissions import (
    ROLE_PERMISSIONS,
    Permission,
    accessible_predio_ids,
    can_access_predio,
    has_permission,
    permissions_for,
    roles_on_predio,
)
from app.shared.roles import Role

PREDIO_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
PREDIO_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


@dataclass
class _Grant:
    """Stands in for UserPredioRole, which satisfies the same protocol."""

    predio_id: uuid.UUID
    role: Role
    expires_at: datetime | None = None


# --- invariants ---------------------------------------------------------------


def test_every_role_has_an_entry() -> None:
    assert set(ROLE_PERMISSIONS) == set(Role)


def test_auditor_holds_no_write_permission() -> None:
    # The auditor reads everything and writes nothing. Any permission that is
    # neither an audit permission nor a view is a write.
    offending = [
        permission
        for permission in ROLE_PERMISSIONS[Role.AUDITOR]
        if not (
            permission.value.startswith("audit:") or permission.value.endswith(":view")
        )
    ]
    assert offending == [], f"auditor must stay read-only, found: {offending}"


def test_planning_and_executing_a_mission_are_never_the_same_role() -> None:
    for role, permissions in ROLE_PERMISSIONS.items():
        both = (
            Permission.MISSION_PLAN in permissions
            and Permission.MISSION_EXECUTE in permissions
        )
        assert not both, f"{role} both plans and executes missions"


def test_approving_and_executing_an_application_are_never_the_same_role() -> None:
    for role, permissions in ROLE_PERMISSIONS.items():
        both = (
            Permission.APPLICATION_APPROVE in permissions
            and Permission.APPLICATION_EXECUTE in permissions
        )
        assert not both, f"{role} both approves and executes applications"


def test_no_role_holds_every_permission() -> None:
    # "No superadministrator" is a design principle, not a preference.
    for role, permissions in ROLE_PERMISSIONS.items():
        assert permissions != frozenset(Permission), f"{role} is a superadministrator"


# --- per-role expectations ----------------------------------------------------


@pytest.mark.parametrize(
    ("role", "granted", "denied"),
    [
        (
            Role.PROPIETARIO,
            [Permission.PREDIO_ASSIGN_USERS, Permission.RESPONSIBLE_MODE_CONFIGURE],
            [
                Permission.MISSION_EXECUTE,
                Permission.APPLICATION_EXECUTE,
                Permission.AUDIT_EXPORT,
            ],
        ),
        (
            Role.ADMIN_OPERACIONES,
            [Permission.MISSION_PLAN, Permission.APPLICATION_APPROVE],
            [
                Permission.MISSION_EXECUTE,
                Permission.APPLICATION_EXECUTE,
                Permission.USER_MANAGE_ROLES,
            ],
        ),
        (
            Role.AGRONOMO,
            [Permission.ANALYSIS_REPORT, Permission.ANALYSIS_VIEW],
            [
                Permission.APPLICATION_APPROVE,
                Permission.APPLICATION_EXECUTE,
                Permission.MISSION_PLAN,
            ],
        ),
        (
            Role.OPERADOR_DRONE,
            [Permission.MISSION_EXECUTE, Permission.CAPTURE_UPLOAD],
            [Permission.MISSION_PLAN, Permission.APPLICATION_EXECUTE],
        ),
        (
            Role.APLICADOR,
            [Permission.APPLICATION_EXECUTE],
            [Permission.APPLICATION_APPROVE, Permission.MISSION_PLAN],
        ),
        (
            Role.BODEGUERO,
            [Permission.INVENTORY_MOVE_IN, Permission.INVENTORY_MOVE_OUT],
            [Permission.INVENTORY_MOVE_RESTRICTED, Permission.INVENTORY_APPROVE],
        ),
        (
            Role.JEFE_BODEGA,
            [Permission.INVENTORY_MOVE_RESTRICTED, Permission.INVENTORY_APPROVE],
            [Permission.MISSION_EXECUTE, Permission.USER_MANAGE_ROLES],
        ),
        (
            Role.AUDITOR,
            [Permission.AUDIT_VIEW_ALL, Permission.AUDIT_EXPORT, Permission.PREDIO_VIEW],
            [
                Permission.PREDIO_UPDATE,
                Permission.INVENTORY_MOVE_IN,
                Permission.MISSION_PLAN,
            ],
        ),
    ],
)
def test_role_grants_and_denials(
    role: Role, granted: list[Permission], denied: list[Permission]
) -> None:
    for permission in granted:
        assert has_permission([role], permission), f"{role} should hold {permission}"
    for permission in denied:
        assert not has_permission([role], permission), (
            f"{role} must not hold {permission}"
        )


def test_jefe_bodega_supersedes_bodeguero() -> None:
    assert ROLE_PERMISSIONS[Role.BODEGUERO] <= ROLE_PERMISSIONS[Role.JEFE_BODEGA]


def test_apicultor_has_no_predio_permissions() -> None:
    assert ROLE_PERMISSIONS[Role.APICULTOR] == frozenset()


# --- accumulation -------------------------------------------------------------


def test_a_user_without_roles_has_no_permissions() -> None:
    assert permissions_for([]) == frozenset()
    assert has_permission([], Permission.PREDIO_VIEW) is False


def test_several_roles_accumulate_their_permissions() -> None:
    roles = [Role.OPERADOR_DRONE, Role.BODEGUERO]
    assert has_permission(roles, Permission.MISSION_EXECUTE) is True
    assert has_permission(roles, Permission.INVENTORY_MOVE_IN) is True
    # Accumulating roles still grants nothing neither of them holds.
    assert has_permission(roles, Permission.APPLICATION_APPROVE) is False


# --- ABAC: predio scoping -----------------------------------------------------


def test_access_is_granted_on_an_assigned_predio() -> None:
    grants = [_Grant(PREDIO_A, Role.APLICADOR)]
    assert can_access_predio(grants, PREDIO_A, Permission.APPLICATION_EXECUTE) is True


def test_access_is_denied_on_someone_elses_predio() -> None:
    # IDOR: the same user, the same role, a predio they were never assigned.
    grants = [_Grant(PREDIO_A, Role.APLICADOR)]
    assert can_access_predio(grants, PREDIO_B, Permission.APPLICATION_EXECUTE) is False


def test_access_is_denied_for_a_permission_the_role_lacks() -> None:
    grants = [_Grant(PREDIO_A, Role.APLICADOR)]
    assert can_access_predio(grants, PREDIO_A, Permission.APPLICATION_APPROVE) is False


def test_an_expired_grant_confers_nothing() -> None:
    expired = _Grant(PREDIO_A, Role.APLICADOR, datetime.now(UTC) - timedelta(minutes=1))
    assert can_access_predio([expired], PREDIO_A, Permission.APPLICATION_EXECUTE) is False
    assert roles_on_predio([expired], PREDIO_A) == frozenset()


def test_a_grant_expiring_later_still_works() -> None:
    future = _Grant(PREDIO_A, Role.AUDITOR, datetime.now(UTC) + timedelta(days=1))
    assert can_access_predio([future], PREDIO_A, Permission.AUDIT_VIEW_PREDIO) is True


def test_a_global_role_applies_to_every_predio() -> None:
    # A system-wide auditor reads predios they hold no individual grant on.
    assert (
        can_access_predio(
            [], PREDIO_B, Permission.AUDIT_VIEW_ALL, global_roles=[Role.AUDITOR]
        )
        is True
    )


def test_a_global_role_still_cannot_write() -> None:
    assert (
        can_access_predio(
            [], PREDIO_B, Permission.PREDIO_UPDATE, global_roles=[Role.AUDITOR]
        )
        is False
    )


def test_roles_from_different_predios_do_not_leak() -> None:
    grants = [_Grant(PREDIO_A, Role.JEFE_BODEGA), _Grant(PREDIO_B, Role.BODEGUERO)]
    assert can_access_predio(grants, PREDIO_A, Permission.INVENTORY_APPROVE) is True
    assert can_access_predio(grants, PREDIO_B, Permission.INVENTORY_APPROVE) is False


def test_accessible_predios_exclude_expired_grants() -> None:
    grants = [
        _Grant(PREDIO_A, Role.APLICADOR),
        _Grant(PREDIO_B, Role.APLICADOR, datetime.now(UTC) - timedelta(seconds=1)),
    ]
    assert accessible_predio_ids(grants) == frozenset({PREDIO_A})


def test_the_real_model_satisfies_the_grant_protocol() -> None:
    # shared/ deliberately does not import module models, so the coupling is
    # structural. This test is what keeps that claim honest.
    from app.modules.users.models import UserPredioRole

    grant = UserPredioRole(
        user_id=uuid.uuid4(),
        predio_id=PREDIO_A,
        role=Role.JEFE_BODEGA,
        expires_at=None,
    )
    assert can_access_predio([grant], PREDIO_A, Permission.INVENTORY_APPROVE) is True
    assert can_access_predio([grant], PREDIO_B, Permission.INVENTORY_APPROVE) is False
