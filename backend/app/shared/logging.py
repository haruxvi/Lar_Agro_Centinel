"""Structured logging.

JSON in production so records are queryable, human-readable in development.
Request context (request id, user) is bound per request and appears on every
line emitted while handling it, without threading it through call signatures.
"""

from __future__ import annotations

import logging
import sys
import uuid
from typing import Any

import structlog

from app.shared.config import Settings, get_settings

_configured = False


def configure_logging(settings: Settings | None = None) -> None:
    """Configure structlog and the standard library logging it wraps."""
    global _configured  # noqa: PLW0603 - module-level, idempotent setup
    resolved = settings or get_settings()

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if resolved.log_format == "json"
        else structlog.dev.ConsoleRenderer()
    )

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, resolved.log_level),
        force=True,
    )
    structlog.configure(
        processors=[*shared_processors, structlog.processors.format_exc_info, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, resolved.log_level)
        ),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound logger, configuring logging on first use."""
    if not _configured:
        configure_logging()
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger


def bind_request_context(
    request_id: uuid.UUID, *, user_id: uuid.UUID | None = None
) -> None:
    """Bind per-request fields so every log line carries them."""
    structlog.contextvars.bind_contextvars(request_id=str(request_id))
    if user_id is not None:
        structlog.contextvars.bind_contextvars(user_id=str(user_id))


def clear_request_context() -> None:
    """Drop the per-request fields once the response is on its way."""
    structlog.contextvars.clear_contextvars()
