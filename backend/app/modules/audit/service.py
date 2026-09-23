"""Append-only audit trail.

The service only inserts. It exposes no update or delete, and the database
refuses both anyway (see docs/decisions/ADR-002-audit-log-immutability.md).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Final

from sqlalchemy.orm import Session

from app.modules.audit.models import AuditLog
from app.modules.audit.repository import AuditRepository
from app.shared.enums import AuditEventCategory, AuditEventSeverity, AuditOutcome
from app.shared.roles import Role

logger = logging.getLogger(__name__)

REDACTED: Final = "[REDACTED]"

# Matched as a case-insensitive substring of the key, so `user_password` and
# `X-Auth-Token` are caught as well.
SENSITIVE_KEY_FRAGMENTS: Final = (
    "password",
    "token",
    "secret",
    "authorization",
    "cookie",
    "totp",
    "recovery_code",
    "api_key",
    "private_key",
)


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(fragment in lowered for fragment in SENSITIVE_KEY_FRAGMENTS)


def sanitize_details(value: Any) -> Any:
    """Strip credentials out of audit details, at any depth.

    The audit trail records what happened, never the secrets involved.
    """
    if isinstance(value, dict):
        return {
            key: REDACTED if _is_sensitive(str(key)) else sanitize_details(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_details(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_details(item) for item in value]
    return value


class AuditService:
    """Records security and domain events. Insert-only by design."""

    def __init__(self, session: Session) -> None:
        """Bind the service to a database session."""
        self._session = session
        self._repository = AuditRepository(session)

    def record(
        self,
        *,
        event_category: AuditEventCategory,
        event_type: str,
        severity: AuditEventSeverity,
        action: str,
        outcome: AuditOutcome,
        actor_user_id: uuid.UUID | None = None,
        actor_role: Role | None = None,
        actor_ip: str | None = None,
        actor_user_agent: str | None = None,
        target_resource_type: str | None = None,
        target_resource_id: uuid.UUID | None = None,
        predio_id: uuid.UUID | None = None,
        details: dict[str, Any] | None = None,
        correlation_id: uuid.UUID | None = None,
    ) -> uuid.UUID | None:
        """Append one entry and return its id.

        Never raises: a failure to write the audit trail must not take down the
        request that triggered it. It is logged as CRITICAL instead, because a
        lost audit record is an incident in its own right.
        """
        entry = AuditLog(
            event_category=event_category,
            event_type=event_type,
            severity=severity,
            action=action,
            outcome=outcome,
            actor_user_id=actor_user_id,
            actor_role=actor_role,
            actor_ip=actor_ip,
            actor_user_agent=actor_user_agent,
            target_resource_type=target_resource_type,
            target_resource_id=target_resource_id,
            predio_id=predio_id,
            details=sanitize_details(details or {}),
            correlation_id=correlation_id,
        )
        # Written inside a savepoint: if the insert fails, the surrounding
        # transaction stays usable instead of being marked for rollback.
        savepoint = self._session.begin_nested()
        try:
            self._repository.append(entry)
            savepoint.commit()
        except Exception:
            savepoint.rollback()
            logger.critical(
                "audit entry could not be written: event_type=%s outcome=%s actor=%s",
                event_type,
                outcome,
                actor_user_id,
                exc_info=True,
            )
            return None
        return entry.id

    def record_security(
        self,
        *,
        event_type: str,
        action: str,
        outcome: AuditOutcome,
        severity: AuditEventSeverity = AuditEventSeverity.INFO,
        **kwargs: Any,
    ) -> uuid.UUID | None:
        """Shorthand for the SECURITY category, which most auth events use."""
        return self.record(
            event_category=AuditEventCategory.SECURITY,
            event_type=event_type,
            severity=severity,
            action=action,
            outcome=outcome,
            **kwargs,
        )
