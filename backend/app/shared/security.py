"""JWT helpers shared across modules.

Only token encoding/decoding lives here; authentication rules belong to the
auth module's service layer.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from app.shared.config import get_settings


class TokenError(Exception):
    """Raised when a token cannot be decoded or has expired."""


def create_access_token(
    subject: str,
    claims: dict[str, Any] | None = None,
    expires_delta: timedelta | None = None,
) -> str:
    """Encode a signed JWT for the given subject."""
    settings = get_settings()
    expire_delta = expires_delta or timedelta(minutes=settings.jwt_expire_minutes)
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": subject,
        "iat": int(now.timestamp()),
        "exp": int((now + expire_delta).timestamp()),
    }
    if claims:
        payload.update(claims)
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode a signed JWT, raising ``TokenError`` when invalid."""
    settings = get_settings()
    try:
        decoded: dict[str, Any] = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
    except jwt.PyJWTError as exc:  # pragma: no cover - re-raised as domain error
        raise TokenError(str(exc)) from exc
    return decoded
