"""audit_log is append-only, enforced by both privileges and a trigger.

See docs/decisions/ADR-002-audit-log-immutability.md.
"""

from __future__ import annotations

import uuid

import psycopg
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.modules.audit.models import AuditLog
from app.modules.users.models import User
from app.shared.enums import AuditEventCategory, AuditEventSeverity, AuditOutcome


def _entry(event_type: str = "LOGIN_SUCCESS") -> AuditLog:
    return AuditLog(
        event_category=AuditEventCategory.SECURITY,
        event_type=event_type,
        severity=AuditEventSeverity.INFO,
        action="probe",
        outcome=AuditOutcome.SUCCESS,
    )


def _assert_insufficient_privilege(exc_info: pytest.ExceptionInfo[DBAPIError]) -> None:
    assert isinstance(exc_info.value.orig, psycopg.errors.InsufficientPrivilege), (
        f"expected InsufficientPrivilege, got {type(exc_info.value.orig).__name__}"
    )


def test_runtime_role_can_append(app_session: Session) -> None:
    entry = _entry()
    app_session.add(entry)
    app_session.flush()
    assert entry.id is not None


def test_runtime_role_can_read(app_session: Session) -> None:
    app_session.add(_entry())
    app_session.flush()
    stored = app_session.execute(select(AuditLog)).scalars().all()
    assert len(stored) >= 1


def test_runtime_role_cannot_update(app_session: Session) -> None:
    entry = _entry()
    app_session.add(entry)
    app_session.flush()

    with pytest.raises(DBAPIError) as exc_info:
        app_session.execute(
            text("UPDATE audit_log SET action = 'tampered' WHERE id = :id"),
            {"id": entry.id},
        )
    _assert_insufficient_privilege(exc_info)
    app_session.rollback()


def test_runtime_role_cannot_delete(app_session: Session) -> None:
    entry = _entry()
    app_session.add(entry)
    app_session.flush()

    with pytest.raises(DBAPIError) as exc_info:
        app_session.execute(
            text("DELETE FROM audit_log WHERE id = :id"), {"id": entry.id}
        )
    _assert_insufficient_privilege(exc_info)
    app_session.rollback()


def test_schema_owner_cannot_update_either(db_session: Session) -> None:
    # The owner keeps every table privilege, so only the trigger stops this.
    entry = _entry()
    db_session.add(entry)
    db_session.flush()

    with pytest.raises(DBAPIError) as exc_info:
        db_session.execute(
            text("UPDATE audit_log SET action = 'tampered' WHERE id = :id"),
            {"id": entry.id},
        )
    _assert_insufficient_privilege(exc_info)
    assert "append-only" in str(exc_info.value.orig)
    db_session.rollback()


def test_schema_owner_cannot_delete_either(db_session: Session) -> None:
    entry = _entry()
    db_session.add(entry)
    db_session.flush()

    with pytest.raises(DBAPIError) as exc_info:
        db_session.execute(text("DELETE FROM audit_log WHERE id = :id"), {"id": entry.id})
    _assert_insufficient_privilege(exc_info)
    db_session.rollback()


def test_truncate_is_blocked(db_session: Session) -> None:
    # TRUNCATE does not fire row-level triggers; a statement-level one covers it.
    with pytest.raises(DBAPIError) as exc_info:
        db_session.execute(text("TRUNCATE audit_log"))
    _assert_insufficient_privilege(exc_info)
    db_session.rollback()


def test_runtime_role_still_writes_business_tables(app_session: Session) -> None:
    # Guards against over-restricting the runtime role: everything that is not
    # the audit trail must remain writable.
    user = User(
        email=f"runtime-{uuid.uuid4().hex[:8]}@example.cl",
        password_hash="$argon2id$placeholder",
        full_name="Runtime Role",
    )
    app_session.add(user)
    app_session.flush()

    app_session.execute(
        text("UPDATE users SET full_name = :name WHERE id = :id"),
        {"name": "Renamed", "id": user.id},
    )
    app_session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user.id})
    app_session.flush()
