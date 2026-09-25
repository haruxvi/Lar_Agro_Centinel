"""Data access for the users context."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.modules.users.models import User, UserPredioRole, UserRole
from app.shared.roles import Role


class UserRepository:
    """Reads and writes ``users`` rows."""

    def __init__(self, session: Session) -> None:
        """Bind the repository to a database session."""
        self._session = session

    def add(self, user: User) -> User:
        """Insert a new user and flush it."""
        self._session.add(user)
        self._session.flush()
        return user

    def get_by_id(self, user_id: uuid.UUID) -> User | None:
        """Return a user by primary key, or ``None``."""
        return self._session.get(User, user_id)

    def get_by_email(self, email: str) -> User | None:
        """Return a user by email (case-insensitive), or ``None``."""
        statement = select(User).where(User.email == email.strip().lower())
        return self._session.execute(statement).scalar_one_or_none()

    def get_by_rut(self, rut: str) -> User | None:
        """Return a user by RUT, or ``None``."""
        statement = select(User).where(User.rut == rut)
        return self._session.execute(statement).scalar_one_or_none()


class UserRoleRepository:
    """Reads and writes role grants, both global and predio-scoped."""

    def __init__(self, session: Session) -> None:
        """Bind the repository to a database session."""
        self._session = session

    def list_global_roles(self, user_id: uuid.UUID) -> Sequence[UserRole]:
        """Return the user's non-expired global role grants."""
        statement = select(UserRole).where(
            UserRole.user_id == user_id,
            or_(UserRole.expires_at.is_(None), UserRole.expires_at > datetime.now(UTC)),
        )
        return self._session.execute(statement).scalars().all()

    def list_predio_roles(self, user_id: uuid.UUID) -> Sequence[UserPredioRole]:
        """Return the user's non-expired predio-scoped role grants."""
        statement = select(UserPredioRole).where(
            UserPredioRole.user_id == user_id,
            or_(
                UserPredioRole.expires_at.is_(None),
                UserPredioRole.expires_at > datetime.now(UTC),
            ),
        )
        return self._session.execute(statement).scalars().all()

    def grant_global(self, grant: UserRole) -> UserRole:
        """Insert a global role grant and flush it."""
        self._session.add(grant)
        self._session.flush()
        return grant

    def grant_predio(self, grant: UserPredioRole) -> UserPredioRole:
        """Insert a predio-scoped role grant and flush it."""
        self._session.add(grant)
        self._session.flush()
        return grant

    def list_for_predio(
        self, predio_id: uuid.UUID
    ) -> Sequence[tuple[UserPredioRole, User]]:
        """Return the non-expired grants on a predio with their users."""
        statement = (
            select(UserPredioRole, User)
            .join(User, User.id == UserPredioRole.user_id)
            .where(
                UserPredioRole.predio_id == predio_id,
                or_(
                    UserPredioRole.expires_at.is_(None),
                    UserPredioRole.expires_at > datetime.now(UTC),
                ),
            )
            .order_by(User.full_name, User.id, UserPredioRole.role)
        )
        return [(row[0], row[1]) for row in self._session.execute(statement)]

    def get_predio_grant(
        self, user_id: uuid.UUID, predio_id: uuid.UUID, role: Role
    ) -> UserPredioRole | None:
        """Return a grant whether or not it has expired: the pair is unique."""
        statement = select(UserPredioRole).where(
            UserPredioRole.user_id == user_id,
            UserPredioRole.predio_id == predio_id,
            UserPredioRole.role == role,
        )
        return self._session.execute(statement).scalar_one_or_none()

    def count_live_predio_role(self, predio_id: uuid.UUID, role: Role) -> int:
        """Return how many users hold ``role`` on a predio right now."""
        statement = select(func.count()).where(
            UserPredioRole.predio_id == predio_id,
            UserPredioRole.role == role,
            or_(
                UserPredioRole.expires_at.is_(None),
                UserPredioRole.expires_at > datetime.now(UTC),
            ),
        )
        return int(self._session.execute(statement).scalar_one())

    def revoke_predio(self, grant: UserPredioRole) -> None:
        """Remove a predio-scoped grant. The audit trail keeps the record."""
        self._session.delete(grant)
        self._session.flush()
