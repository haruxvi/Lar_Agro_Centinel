"""Persistence behaviour of the users schema."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.users.models import User, UserPredioRole, UserRole
from app.shared.roles import Role
from tests.fixtures.predios import create_predio


def _user(email: str = "propietaria@example.cl", rut: str | None = None) -> User:
    return User(
        email=email,
        password_hash="$argon2id$placeholder",
        full_name="Ada Lovelace",
        rut=rut,
    )


def test_user_is_persisted_with_safe_defaults(db_session: Session) -> None:
    user = _user()
    db_session.add(user)
    db_session.flush()

    stored = db_session.execute(select(User).where(User.id == user.id)).scalar_one()
    assert stored.is_active is True
    assert stored.email_verified is False
    assert stored.failed_login_attempts == 0
    assert stored.locked_until is None
    assert stored.last_login_at is None
    assert stored.created_at is not None
    assert stored.created_at.tzinfo is not None


def test_email_is_unique(db_session: Session) -> None:
    db_session.add(_user(email="dup@example.cl"))
    db_session.flush()

    db_session.add(_user(email="dup@example.cl"))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_rut_is_unique(db_session: Session) -> None:
    db_session.add(_user(email="one@example.cl", rut="12345678-5"))
    db_session.flush()

    db_session.add(_user(email="two@example.cl", rut="12345678-5"))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_rut_is_optional(db_session: Session) -> None:
    db_session.add(_user(email="a@example.cl", rut=None))
    db_session.add(_user(email="b@example.cl", rut=None))
    db_session.flush()  # two NULL RUTs do not collide


def test_user_accumulates_several_global_roles(db_session: Session) -> None:
    user = _user()
    db_session.add(user)
    db_session.flush()

    db_session.add(UserRole(user_id=user.id, role=Role.AGRONOMO))
    db_session.add(UserRole(user_id=user.id, role=Role.AUDITOR))
    db_session.flush()
    db_session.refresh(user)

    assert {grant.role for grant in user.roles} == {Role.AGRONOMO, Role.AUDITOR}


def test_same_global_role_cannot_be_granted_twice(db_session: Session) -> None:
    user = _user()
    db_session.add(user)
    db_session.flush()

    db_session.add(UserRole(user_id=user.id, role=Role.AGRONOMO))
    db_session.flush()
    db_session.add(UserRole(user_id=user.id, role=Role.AGRONOMO))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_role_grant_records_who_granted_it_and_when_it_expires(
    db_session: Session,
) -> None:
    owner = _user(email="owner@example.cl")
    auditor = _user(email="auditor@example.cl")
    db_session.add_all([owner, auditor])
    db_session.flush()

    expires = datetime.now(UTC) + timedelta(days=30)
    db_session.add(
        UserRole(
            user_id=auditor.id,
            role=Role.AUDITOR,
            granted_by_user_id=owner.id,
            expires_at=expires,
        )
    )
    db_session.flush()

    grant = db_session.execute(
        select(UserRole).where(UserRole.user_id == auditor.id)
    ).scalar_one()
    assert grant.granted_by_user_id == owner.id
    assert grant.expires_at is not None
    assert grant.granted_at is not None


def test_roles_can_be_scoped_to_different_predios(db_session: Session) -> None:
    user = _user()
    db_session.add(user)
    db_session.flush()

    predio_a = create_predio(db_session, user.id).id
    predio_b = create_predio(db_session, user.id).id
    db_session.add(
        UserPredioRole(user_id=user.id, predio_id=predio_a, role=Role.APLICADOR)
    )
    db_session.add(
        UserPredioRole(user_id=user.id, predio_id=predio_b, role=Role.APLICADOR)
    )
    db_session.flush()
    db_session.refresh(user)

    assert {grant.predio_id for grant in user.predio_roles} == {predio_a, predio_b}


def test_same_role_on_same_predio_cannot_be_granted_twice(db_session: Session) -> None:
    user = _user()
    db_session.add(user)
    db_session.flush()

    predio_id = create_predio(db_session, user.id).id
    db_session.add(
        UserPredioRole(user_id=user.id, predio_id=predio_id, role=Role.BODEGUERO)
    )
    db_session.flush()
    db_session.add(
        UserPredioRole(user_id=user.id, predio_id=predio_id, role=Role.BODEGUERO)
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_deleting_a_user_removes_their_grants(db_session: Session) -> None:
    user = _user()
    db_session.add(user)
    db_session.flush()
    db_session.add(UserRole(user_id=user.id, role=Role.APICULTOR))
    # Owned by someone else: an owner cannot be deleted while owning predios.
    owner = _user(email="predio-owner@example.cl")
    db_session.add(owner)
    db_session.flush()
    predio_id = create_predio(db_session, owner.id).id
    db_session.add(
        UserPredioRole(user_id=user.id, predio_id=predio_id, role=Role.APLICADOR)
    )
    db_session.flush()

    db_session.delete(user)
    db_session.flush()

    assert (
        db_session.execute(select(UserRole).where(UserRole.user_id == user.id)).first()
        is None
    )
    assert (
        db_session.execute(
            select(UserPredioRole).where(UserPredioRole.user_id == user.id)
        ).first()
        is None
    )
