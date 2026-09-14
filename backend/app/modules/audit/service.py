"""Application services of the audit context."""

from __future__ import annotations

from app.modules.audit.repository import AuditRepository


class AuditService:
    """Entry point of the audit context's public API."""

    def __init__(self, repository: AuditRepository) -> None:
        """Bind the service to its repository."""
        self._repository = repository
