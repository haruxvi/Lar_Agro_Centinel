"""JWT helpers shared across modules.

Only token encoding/decoding lives here; authentication rules belong to the
auth module's service layer.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from app.shared.config import get_settings

_REQUIRED_CLAIMS = ["exp", "iat", "iss", "sub"]


class TokenError(Exception):
    """Raised when a token cannot be decoded or has expired."""


def create_access_token(
    subject: str,
    claims: dict[str, Any] | None = None,
    expires_delta: timedelta | None = None,
) -> str:
    """Encode a signed JWT for the given subject."""
    settings = get_settings()
    expire_delta = expires_delta or timedelta(
        minutes=settings.jwt_access_token_expire_minutes
    )
    now = datetime.now(UTC)
    payload: dict[str, Any] = dict(claims or {})
    payload.update(
        {
            "sub": subject,
            "iss": settings.jwt_issuer,
            "iat": int(now.timestamp()),
            "exp": int((now + expire_delta).timestamp()),
        }
    )
    return jwt.encode(
        payload,
        settings.secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode a signed JWT, raising ``TokenError`` when invalid."""
    settings = get_settings()
    try:
        decoded: dict[str, Any] = jwt.decode(
            token,
            settings.secret_key.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
            issuer=settings.jwt_issuer,
            leeway=settings.jwt_leeway_seconds,
            options={"require": _REQUIRED_CLAIMS},
        )
    except jwt.PyJWTError as exc:
        raise TokenError(str(exc)) from exc
    return decoded
