"""Data access for the auth context."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.auth.models import User


class UserRepository:
    """Reads and writes ``users`` rows."""

    def __init__(self, session: Session) -> None:
        """Bind the repository to a database session."""
        self._session = session

    def get_by_id(self, user_id: uuid.UUID) -> User | None:
        """Return a user by primary key, or ``None``."""
        return self._session.get(User, user_id)

    def get_by_email(self, email: str) -> User | None:
        """Return a user by email, or ``None``."""
        statement = select(User).where(User.email == email)
        return self._session.execute(statement).scalar_one_or_none()
