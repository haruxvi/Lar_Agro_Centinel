"""ASGI middleware: security headers and per-request context."""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Final

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.shared.logging import bind_request_context, clear_request_context, get_logger

REQUEST_ID_HEADER: Final = "X-Request-ID"

# Conservative by default: this API serves JSON to a separate frontend, so it
# needs no framing, no sniffing, no cross-domain policy files and no scripts.
SECURITY_HEADERS: Final = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Content-Security-Policy": "default-src 'self'",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    "X-Permitted-Cross-Domain-Policies": "none",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to every response, including error responses."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Attach the headers on the way out."""
        response = await call_next(request)
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        return response


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Give each request an id, echo it back, and log how long it took."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Bind the request context, time the request and log the outcome."""
        request_id = uuid.uuid4()
        request.state.request_id = request_id
        bind_request_context(request_id)
        logger = get_logger("app.request")
        started = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.exception(
                "request_failed",
                method=request.method,
                path=request.url.path,
                duration_ms=duration_ms,
            )
            clear_request_context()
            raise

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.info(
            "request_completed",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=duration_ms,
        )
        response.headers[REQUEST_ID_HEADER] = str(request_id)
        clear_request_context()
        return response
