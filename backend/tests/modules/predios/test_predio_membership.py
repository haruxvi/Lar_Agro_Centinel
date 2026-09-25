"""Who may act on a predio: granting and revoking predio-scoped roles."""

from __future__ import annotations

import json
import random
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.audit import events
from app.modules.audit.models import AuditLog
from app.modules.predios.exceptions import (
    AssignmentExistsError,
    AssignmentNotFoundError,
    LastPropietarioError,
    MemberNotFoundError,
    OwnerRoleProtectedError,
    PredioAccessDeniedError,
    PredioError,
    RoleNotAssignableError,
)
from app.modules.predios.models import Predio
from app.modules.predios.service import Actor, PredioService
from app.modules.users.models import User, UserPredioRole, UserRole
from app.shared.enums import AuditEventCategory, AuditEventSeverity
from app.shared.roles import Role
from tests.fixtures import geometries as g
from tests.modules.predios.conftest import MakeUser

TOMORROW = datetime.now(UTC) + timedelta(days=1)


@pytest.fixture
def service(db_session: Session) -> PredioService:
    return PredioService(db_session)


@pytest.fixture
def owner(db_session: Session, make_user: MakeUser) -> User:
    user = make_user()
    db_session.add(UserRole(user_id=user.id, role=Role.PROPIETARIO))
    db_session.flush()
    return user


@pytest.fixture
def actor(owner: User) -> Actor:
    return Actor(user_id=owner.id, role=Role.PROPIETARIO)


@pytest.fixture
def predio(service: PredioService, actor: Actor) -> Predio:
    return service.create_predio(
        actor, geojson=g.SQUARE, fields={"name": "Equipo"}
    ).predio


def _email_of(service: PredioService, actor: Actor) -> str:
    user = service._users.get_by_id(actor.user_id)
    assert user is not None
    return user.email


def _live_propietarios(session: Session, predio_id: uuid.UUID) -> int:
    now = datetime.now(UTC)
    grants = session.execute(
        select(UserPredioRole).where(
            UserPredioRole.predio_id == predio_id,
            UserPredioRole.role == Role.PROPIETARIO,
        )
    ).scalars()
    return sum(
        1 for grant in grants if grant.expires_at is None or grant.expires_at > now
    )


# --- assigning ----------------------------------------------------------------


def test_a_propietario_assigns_a_worker_by_email(
    db_session: Session,
    service: PredioService,
    actor: Actor,
    predio: Predio,
    make_user: MakeUser,
) -> None:
    worker = make_user()
    grant, user = service.assign_member(
        actor, predio.id, email=worker.email.upper(), role=Role.APLICADOR
    )

    assert (user.id, grant.role, grant.granted_by_user_id) == (
        worker.id,
        Role.APLICADOR,
        actor.user_id,
    )
    entry = db_session.execute(
        select(AuditLog).where(
            AuditLog.event_type == events.PREDIO_USER_ASSIGNED,
            AuditLog.predio_id == predio.id,
        )
    ).scalar_one()
    assert entry.event_category is AuditEventCategory.SECURITY
    assert entry.severity is AuditEventSeverity.WARNING
    assert entry.target_resource_id == worker.id
    assert entry.details["role"] == "APLICADOR"
    # The audit trail identifies people by id, not by their email address.
    assert worker.email not in json.dumps(entry.details)


def test_the_new_member_can_read_the_predio(
    service: PredioService, actor: Actor, predio: Predio, make_user: MakeUser
) -> None:
    worker = make_user()
    service.assign_member(actor, predio.id, email=worker.email, role=Role.AGRONOMO)
    assert service.get_predio(Actor(user_id=worker.id), predio.id) == predio


def test_apicultor_cannot_be_assigned_to_a_predio(
    service: PredioService, actor: Actor, predio: Predio, make_user: MakeUser
) -> None:
    with pytest.raises(RoleNotAssignableError) as caught:
        service.assign_member(
            actor, predio.id, email=make_user().email, role=Role.APICULTOR
        )
    assert caught.value.role == "APICULTOR"


@pytest.mark.parametrize(
    ("role", "expires_at", "reason"),
    [
        (Role.PROPIETARIO, TOMORROW, "PROPIETARIO grants do not expire"),
        (Role.APLICADOR, datetime.now(UTC) - timedelta(minutes=1), "in the future"),
        (Role.APLICADOR, datetime(2099, 1, 1), "timezone"),  # noqa: DTZ001
    ],
)
def test_invalid_expiries_are_refused(
    service: PredioService,
    actor: Actor,
    predio: Predio,
    make_user: MakeUser,
    role: Role,
    expires_at: datetime,
    reason: str,
) -> None:
    with pytest.raises(RoleNotAssignableError, match=reason):
        service.assign_member(
            actor, predio.id, email=make_user().email, role=role, expires_at=expires_at
        )


def test_an_unknown_email_is_not_found(
    service: PredioService, actor: Actor, predio: Predio
) -> None:
    with pytest.raises(MemberNotFoundError):
        service.assign_member(
            actor, predio.id, email="nadie@example.cl", role=Role.APLICADOR
        )


def test_a_deactivated_account_is_indistinguishable_from_an_unknown_one(
    db_session: Session,
    service: PredioService,
    actor: Actor,
    predio: Predio,
    make_user: MakeUser,
) -> None:
    worker = make_user()
    worker.is_active = False
    db_session.flush()
    with pytest.raises(MemberNotFoundError):
        service.assign_member(actor, predio.id, email=worker.email, role=Role.APLICADOR)


def test_a_role_already_held_is_a_conflict(
    service: PredioService, actor: Actor, predio: Predio, make_user: MakeUser
) -> None:
    worker = make_user()
    service.assign_member(actor, predio.id, email=worker.email, role=Role.APLICADOR)
    with pytest.raises(AssignmentExistsError):
        service.assign_member(actor, predio.id, email=worker.email, role=Role.APLICADOR)


def test_an_expired_grant_is_renewed(
    db_session: Session,
    service: PredioService,
    actor: Actor,
    predio: Predio,
    make_user: MakeUser,
) -> None:
    worker = make_user()
    db_session.add(
        UserPredioRole(
            user_id=worker.id,
            predio_id=predio.id,
            role=Role.APLICADOR,
            expires_at=datetime.now(UTC) - timedelta(days=1),
        )
    )
    db_session.flush()

    grant, _ = service.assign_member(
        actor, predio.id, email=worker.email, role=Role.APLICADOR, expires_at=TOMORROW
    )
    assert grant.expires_at == TOMORROW
    entry = db_session.execute(
        select(AuditLog).where(
            AuditLog.event_type == events.PREDIO_USER_ASSIGNED,
            AuditLog.predio_id == predio.id,
        )
    ).scalar_one()
    assert entry.details["renewed"] is True


def test_only_holders_of_predio_assign_users_may_assign(
    db_session: Session,
    service: PredioService,
    actor: Actor,
    predio: Predio,
    make_user: MakeUser,
) -> None:
    admin = make_user()
    service.assign_member(
        actor, predio.id, email=admin.email, role=Role.ADMIN_OPERACIONES
    )
    with pytest.raises(PredioAccessDeniedError):
        service.assign_member(
            Actor(user_id=admin.id),
            predio.id,
            email=make_user().email,
            role=Role.APLICADOR,
        )


# --- revoking -----------------------------------------------------------------


def test_revoking_removes_access_and_is_audited(
    db_session: Session,
    service: PredioService,
    actor: Actor,
    predio: Predio,
    make_user: MakeUser,
) -> None:
    worker = make_user()
    service.assign_member(actor, predio.id, email=worker.email, role=Role.APLICADOR)
    service.unassign_member(actor, predio.id, worker.id, Role.APLICADOR)

    with pytest.raises(PredioAccessDeniedError):
        service.get_predio(Actor(user_id=worker.id), predio.id)
    entry = db_session.execute(
        select(AuditLog).where(
            AuditLog.event_type == events.PREDIO_USER_UNASSIGNED,
            AuditLog.predio_id == predio.id,
        )
    ).scalar_one()
    assert entry.details == {
        "user_id": str(worker.id),
        "role": "APLICADOR",
        "self_removal": False,
    }


def test_revoking_a_role_not_held_is_not_found(
    service: PredioService, actor: Actor, predio: Predio, make_user: MakeUser
) -> None:
    with pytest.raises(AssignmentNotFoundError):
        service.unassign_member(actor, predio.id, make_user().id, Role.APLICADOR)


def test_the_last_propietario_cannot_remove_themselves(
    service: PredioService, actor: Actor, predio: Predio
) -> None:
    with pytest.raises(LastPropietarioError):
        service.unassign_member(actor, predio.id, actor.user_id, Role.PROPIETARIO)


def test_one_of_two_propietarios_can_leave(
    db_session: Session,
    service: PredioService,
    actor: Actor,
    predio: Predio,
    make_user: MakeUser,
) -> None:
    # The co-owner leaves; the owner, now alone, cannot. (Before the owner's
    # role was protected, this test had the owner leave instead.)
    partner = make_user()
    service.assign_member(actor, predio.id, email=partner.email, role=Role.PROPIETARIO)

    partner_actor = Actor(user_id=partner.id)
    service.unassign_member(partner_actor, predio.id, partner.id, Role.PROPIETARIO)
    with pytest.raises(LastPropietarioError):
        service.unassign_member(actor, predio.id, actor.user_id, Role.PROPIETARIO)
    assert _live_propietarios(db_session, predio.id) == 1


def test_a_co_owner_cannot_lock_the_owner_out(
    db_session: Session,
    service: PredioService,
    actor: Actor,
    predio: Predio,
    make_user: MakeUser,
) -> None:
    partner = make_user()
    service.assign_member(actor, predio.id, email=partner.email, role=Role.PROPIETARIO)

    with pytest.raises(OwnerRoleProtectedError):
        service.unassign_member(
            Actor(user_id=partner.id), predio.id, actor.user_id, Role.PROPIETARIO
        )
    # Nor can the owner drop it while a co-owner exists: ownership would be
    # left with an account that has no access to the predio.
    with pytest.raises(OwnerRoleProtectedError):
        service.unassign_member(actor, predio.id, actor.user_id, Role.PROPIETARIO)
    assert service.get_predio(actor, predio.id) == predio


def test_the_owner_can_still_lose_other_roles(
    service: PredioService, actor: Actor, predio: Predio
) -> None:
    # Only the owner's PROPIETARIO role is protected, not every grant they hold.
    service.assign_member(
        actor, predio.id, email=_email_of(service, actor), role=Role.AGRONOMO
    )
    service.unassign_member(actor, predio.id, actor.user_id, Role.AGRONOMO)


def test_an_expired_propietario_does_not_count_as_a_second_one(
    db_session: Session,
    service: PredioService,
    actor: Actor,
    predio: Predio,
    make_user: MakeUser,
) -> None:
    db_session.add(
        UserPredioRole(
            user_id=make_user().id,
            predio_id=predio.id,
            role=Role.PROPIETARIO,
            expires_at=datetime.now(UTC) - timedelta(days=1),
        )
    )
    db_session.flush()
    with pytest.raises(LastPropietarioError):
        service.unassign_member(actor, predio.id, actor.user_id, Role.PROPIETARIO)


# --- invariant ----------------------------------------------------------------


@pytest.mark.parametrize("seed", range(5))
def test_a_predio_never_ends_up_without_a_propietario(
    db_session: Session,
    service: PredioService,
    actor: Actor,
    predio: Predio,
    make_user: MakeUser,
    seed: int,
) -> None:
    """Throw random grants and revocations at a predio; the invariant must hold."""
    rng = random.Random(seed)  # noqa: S311 - reproducible test data, not security
    people = [make_user() for _ in range(3)]
    roles = [Role.PROPIETARIO, Role.ADMIN_OPERACIONES, Role.APLICADOR]

    actors = [actor, *(Actor(user_id=person.id) for person in people)]
    refused = 0

    for _ in range(40):
        target, role, acting = rng.choice(people), rng.choice(roles), rng.choice(actors)
        # Refusals (no permission, already held, last PROPIETARIO...) are part
        # of the exercise; what matters is the state after each attempt.
        try:
            if rng.random() < 0.5:
                service.assign_member(acting, predio.id, email=target.email, role=role)
            else:
                service.unassign_member(acting, predio.id, target.id, role)
        except PredioError:
            refused += 1
        assert _live_propietarios(db_session, predio.id) >= 1

    assert refused < 40, "every operation was refused: the test exercised nothing"
