"""Audit bounded context: append-only trail of relevant events."""

from app.modules.audit.router import router

__all__ = ["router"]
