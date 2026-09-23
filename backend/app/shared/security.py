"""JWT issuing and verification.

Fail-closed by construction: ``decode_token`` either returns a fully validated
payload or raises. It never returns partial data, and it never falls back to a
default when a claim is missing or malformed.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import jwt
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.shared.config import get_settings
from app.shared.roles import Role

AUDIENCE: Final = "lar-agro-centinel-api"

PURPOSE_ACCESS: Final = "access"
PURPOSE_TWO_FACTOR: Final = "2fa"

_REQUIRED_CLAIMS: Final = ["sub", "iss", "aud", "iat", "exp", "jti"]
_REFRESH_TOKEN_BYTES: Final = 48


class InvalidTokenError(Exception):
    """Raised whenever a token cannot be fully validated."""


class TokenPayload(BaseModel):
    """A decoded token. Every field has been validated."""

    model_config = ConfigDict(frozen=True)

    sub: uuid.UUID
    iss: str
    aud: str
    iat: int
    exp: int
    jti: uuid.UUID
    roles: list[Role] = Field(default_factory=list)
    active_role: Role | None = None
    predio_ids: list[uuid.UUID] = Field(default_factory=list)
    two_factor_verified: bool = Field(default=False, alias="2fa_verified")
    purpose: str = PURPOSE_ACCESS

    @property
    def user_id(self) -> uuid.UUID:
        """Return the subject as a user identifier."""
        return self.sub


def _now() -> datetime:
    return datetime.now(UTC)


def create_access_token(
    user_id: uuid.UUID,
    roles: list[Role],
    active_role: Role | None = None,
    predio_ids: list[uuid.UUID] | None = None,
    *,
    two_factor_verified: bool = False,
    expires_delta: timedelta | None = None,
) -> str:
    """Issue a signed access token for an authenticated session."""
    settings = get_settings()
    now = _now()
    delta = expires_delta or timedelta(minutes=settings.jwt_access_token_expire_minutes)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "iss": settings.jwt_issuer,
        "aud": AUDIENCE,
        "iat": int(now.timestamp()),
        "exp": int((now + delta).timestamp()),
        # Recorded so individual tokens can be revoked in a later phase.
        "jti": str(uuid.uuid4()),
        "roles": [role.value for role in roles],
        "active_role": active_role.value if active_role else None,
        "predio_ids": [str(predio_id) for predio_id in (predio_ids or [])],
        "2fa_verified": two_factor_verified,
        "purpose": PURPOSE_ACCESS,
    }
    return jwt.encode(
        payload,
        settings.secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )


def create_challenge_token(user_id: uuid.UUID, expires_delta: timedelta) -> str:
    """Issue a short-lived token that only proves the password step passed.

    A challenge token is not a session: it carries no roles, and the
    dependencies reject it wherever an access token is expected.
    """
    settings = get_settings()
    now = _now()
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "iss": settings.jwt_issuer,
        "aud": AUDIENCE,
        "iat": int(now.timestamp()),
        "exp": int((now + expires_delta).timestamp()),
        "jti": str(uuid.uuid4()),
        "purpose": PURPOSE_TWO_FACTOR,
    }
    return jwt.encode(
        payload,
        settings.secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )


def hash_refresh_token(plain: str) -> str:
    """Return the SHA-256 hex digest stored for a refresh token."""
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def create_refresh_token(user_id: uuid.UUID) -> tuple[str, str]:
    """Return ``(plaintext, hash)`` for a new refresh token.

    Opaque random value rather than a JWT: it is looked up in the database on
    every use, so it gains nothing from being self-describing, and only the
    hash is ever stored.
    """
    del user_id  # bound to the user by the row that stores the hash
    plain = secrets.token_urlsafe(_REFRESH_TOKEN_BYTES)
    return plain, hash_refresh_token(plain)


def decode_token(token: str) -> TokenPayload:
    """Decode and fully validate a token.

    Raises ``InvalidTokenError`` when anything at all fails: a bad signature, an
    unexpected algorithm, a wrong issuer or audience, an expired or
    future-dated token, or a claim that is missing, malformed or unknown.
    """
    settings = get_settings()
    try:
        raw: dict[str, Any] = jwt.decode(
            token,
            settings.secret_key.get_secret_value(),
            # Pinned to the configured algorithm: the header's `alg` is never
            # trusted, so `none` and algorithm-confusion attempts are rejected.
            algorithms=[settings.jwt_algorithm],
            issuer=settings.jwt_issuer,
            audience=AUDIENCE,
            leeway=settings.jwt_leeway_seconds,
            options={"require": _REQUIRED_CLAIMS, "verify_signature": True},
        )
    except jwt.PyJWTError as exc:
        raise InvalidTokenError(str(exc)) from exc

    # PyJWT accepts an `iat` in the future; a token minted ahead of time is not
    # something this system ever issues.
    issued_at = raw.get("iat")
    if not isinstance(issued_at, int):
        raise InvalidTokenError("iat claim is not an integer")
    if issued_at > int(_now().timestamp()) + settings.jwt_leeway_seconds:
        raise InvalidTokenError("iat claim is in the future")

    try:
        payload = TokenPayload.model_validate(raw)
    except ValidationError as exc:
        raise InvalidTokenError(f"token claims are invalid: {exc.error_count()}") from exc

    if payload.purpose == PURPOSE_ACCESS:
        if payload.roles and payload.active_role is None:
            raise InvalidTokenError("access token has no active_role")
        if payload.active_role is not None and payload.active_role not in payload.roles:
            raise InvalidTokenError("active_role is not among the granted roles")

    return payload
