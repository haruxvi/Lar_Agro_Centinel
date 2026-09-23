"""Rate limiting.

Keyed by authenticated user when there is one and by IP otherwise: limiting
only by IP punishes everyone behind a shared address, and limiting only by user
leaves anonymous endpoints (login, registration) unprotected.
"""

from __future__ import annotations

from typing import Final

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

from app.shared.config import get_settings

REGISTRATION_RATE_LIMIT: Final = "3/hour"


def rate_limit_key(request: Request) -> str:
    """Return the bucket a request counts against."""
    user_id = getattr(request.state, "user_id", None)
    if user_id is not None:
        return f"user:{user_id}"
    return get_remote_address(request)


def default_limit() -> str:
    """Return the limit applied to every endpoint without its own."""
    return get_settings().rate_limit_default


def auth_limit() -> str:
    """Return the limit applied to credential checks."""
    return get_settings().rate_limit_auth


def two_factor_limit() -> str:
    """Return the limit applied to second-factor endpoints."""
    return get_settings().rate_limit_2fa


def registration_limit() -> str:
    """Return the limit applied to self-registration."""
    return REGISTRATION_RATE_LIMIT


def build_limiter() -> Limiter:
    """Build the limiter, backed by Redis so limits hold across workers."""
    settings = get_settings()
    return Limiter(
        key_func=rate_limit_key,
        default_limits=[settings.rate_limit_default],
        storage_uri=str(settings.redis_url),
        enabled=settings.rate_limit_enabled,
        headers_enabled=True,
    )


limiter = build_limiter()
"""Module-level instance: the route decorators need it at import time."""
