"""ASGI middleware: security headers, per-request context and body size limits."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Final

from starlette.datastructures import Headers
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

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


class BodySizeLimitMiddleware:
    """Refuse oversized request bodies under a path prefix with a 413.

    Pure ASGI on purpose: the body is counted as it arrives, before FastAPI
    parses a byte of JSON, and a lying or missing ``Content-Length`` does not
    get around it. Bodies under the prefix are buffered, which is acceptable
    because they are capped.
    """

    def __init__(
        self, app: ASGIApp, *, path_prefix: str, max_bytes: Callable[[], int]
    ) -> None:
        """Wrap ``app``; ``max_bytes`` is read per request so settings apply."""
        self.app = app
        self.path_prefix = path_prefix
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Count the body and either replay it to the app or answer 413."""
        if scope["type"] != "http" or not scope["path"].startswith(self.path_prefix):
            await self.app(scope, receive, send)
            return

        limit = self.max_bytes()
        declared = Headers(scope=scope).get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            await self._reject(int(declared), limit, send)
            return

        chunks: list[bytes] = []
        size = 0
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > limit:
                await self._reject(size, limit, send)
                return
            chunks.append(chunk)
            more = message.get("more_body", False)

        body = b"".join(chunks)
        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if replayed:
                return await receive()
            replayed = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay, send)

    @staticmethod
    async def _reject(size: int, limit: int, send: Send) -> None:
        payload = json.dumps(
            {
                "detail": {
                    "error": "PayloadTooLargeError",
                    "size_bytes": size,
                    "limit_bytes": limit,
                }
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})
