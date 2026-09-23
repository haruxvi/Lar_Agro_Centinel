"""Persistence models for the auth context."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.db import Base, IPAddress


class UserTwoFactor(Base):
    """TOTP enrolment state for a user."""

    __tablename__ = "user_two_factor"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    # Fernet ciphertext. The TOTP secret is never stored in the clear.
    totp_secret_encrypted: Mapped[str] = mapped_column(String(512))
    totp_verified: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    # SHA-256 of each recovery code; the plaintext is shown once and discarded.
    recovery_codes_hash: Mapped[list[str]] = mapped_column(
        ARRAY(String(64)), default=list, server_default=text("'{}'::varchar[]")
    )
    enrolled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    failed_attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )


class RefreshToken(Base):
    """A refresh token issued to a user session."""

    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # SHA-256 of the token; the plaintext only ever reaches the client.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    revoked_reason: Mapped[str | None] = mapped_column(String(64), default=None)
    user_agent: Mapped[str | None] = mapped_column(String(512), default=None)
    ip_address: Mapped[str | None] = mapped_column(IPAddress, default=None)
