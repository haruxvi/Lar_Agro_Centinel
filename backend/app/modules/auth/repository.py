"""Data access for the auth context."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.auth.models import RefreshToken, UserTwoFactor


class TwoFactorRepository:
    """Reads and writes ``user_two_factor`` rows."""

    def __init__(self, session: Session) -> None:
        """Bind the repository to a database session."""
        self._session = session

    def get_for_user(self, user_id: uuid.UUID) -> UserTwoFactor | None:
        """Return the user's 2FA enrolment, or ``None``."""
        return self._session.get(UserTwoFactor, user_id)

    def save(self, enrolment: UserTwoFactor) -> UserTwoFactor:
        """Insert or update a 2FA enrolment and flush it."""
        merged = self._session.merge(enrolment)
        self._session.flush()
        return merged

    def delete_for_user(self, user_id: uuid.UUID) -> None:
        """Remove a user's 2FA enrolment."""
        enrolment = self.get_for_user(user_id)
        if enrolment is not None:
            self._session.delete(enrolment)
            self._session.flush()


class RefreshTokenRepository:
    """Reads and writes ``refresh_tokens`` rows. Tokens are stored hashed."""

    def __init__(self, session: Session) -> None:
        """Bind the repository to a database session."""
        self._session = session

    def add(self, token: RefreshToken) -> RefreshToken:
        """Insert a refresh token and flush it."""
        self._session.add(token)
        self._session.flush()
        return token

    def get_by_hash(self, token_hash: str) -> RefreshToken | None:
        """Return a refresh token by its SHA-256 hash, or ``None``."""
        statement = select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        return self._session.execute(statement).scalar_one_or_none()

    def list_active_for_user(self, user_id: uuid.UUID) -> Sequence[RefreshToken]:
        """Return the user's tokens that are neither revoked nor expired."""
        statement = select(RefreshToken).where(
            RefreshToken.user_id == user_id,
            RefreshToken.revoked_at.is_(None),
            RefreshToken.expires_at > datetime.now(UTC),
        )
        return self._session.execute(statement).scalars().all()

    def revoke(self, token: RefreshToken, reason: str) -> RefreshToken:
        """Mark one token as revoked."""
        token.revoked_at = datetime.now(UTC)
        token.revoked_reason = reason
        self._session.flush()
        return token

    def revoke_all_for_user(self, user_id: uuid.UUID, reason: str) -> int:
        """Revoke every active token of a user. Returns how many were revoked."""
        tokens = list(self.list_active_for_user(user_id))
        for token in tokens:
            self.revoke(token, reason)
        return len(tokens)
