"""Data access for the users context."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.modules.users.models import User, UserPredioRole, UserRole


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
