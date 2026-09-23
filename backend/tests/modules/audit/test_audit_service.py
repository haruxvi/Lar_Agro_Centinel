"""Audit service: sanitisation, append-only surface and failure behaviour."""

from __future__ import annotations

import logging
import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.audit import events
from app.modules.audit.models import AuditLog
from app.modules.audit.repository import AuditRepository
from app.modules.audit.service import REDACTED, AuditService, sanitize_details
from app.modules.users.models import User
from app.shared.enums import AuditEventCategory, AuditEventSeverity, AuditOutcome

SECRET_VALUE = "super-secret-value"  # noqa: S105 - test fixture, not a credential


# --- sanitisation -------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "token",
        "secret",
        "authorization",
        "cookie",
        "totp",
        "recovery_code",
        "api_key",
        "private_key",
    ],
)
def test_every_sensitive_key_is_redacted(key: str) -> None:
    assert sanitize_details({key: SECRET_VALUE}) == {key: REDACTED}


@pytest.mark.parametrize(
    "key",
    [
        "PASSWORD",
        "User_Password",
        "X-Auth-Token",
        "refresh_token",
        "totp_secret_encrypted",
        "AUTHORIZATION",
        "recovery_codes",
    ],
)
def test_redaction_matches_case_insensitively_and_as_a_substring(key: str) -> None:
    assert sanitize_details({key: SECRET_VALUE})[key] == REDACTED


def test_ordinary_values_survive() -> None:
    details = {"reason": "invalid_password", "attempts": 3, "locked": False}
    assert sanitize_details(details) == details


def test_redaction_reaches_nested_structures() -> None:
    details = {
        "request": {
            "headers": {"authorization": "Bearer abc", "user-agent": "curl/8"},
            "body": {"email": "a@example.cl", "password": SECRET_VALUE},
        },
        "attempts": [
            {"token": "t1", "ok": False},
            {"token": "t2", "ok": True},
        ],
    }
    cleaned = sanitize_details(details)

    assert cleaned["request"]["headers"]["authorization"] == REDACTED
    assert cleaned["request"]["headers"]["user-agent"] == "curl/8"
    assert cleaned["request"]["body"]["password"] == REDACTED
    assert cleaned["request"]["body"]["email"] == "a@example.cl"
    assert [item["token"] for item in cleaned["attempts"]] == [REDACTED, REDACTED]
    assert [item["ok"] for item in cleaned["attempts"]] == [False, True]


def test_tuples_become_json_safe_lists() -> None:
    assert sanitize_details({"codes": ("a", "b")}) == {"codes": ["a", "b"]}


def test_sanitisation_does_not_mutate_the_original() -> None:
    original: dict[str, Any] = {"password": SECRET_VALUE}
    sanitize_details(original)
    assert original["password"] == SECRET_VALUE


def test_recorded_details_reach_the_database_already_sanitised(
    app_session: Session,
) -> None:
    service = AuditService(app_session)
    entry_id = service.record_security(
        event_type=events.LOGIN_FAILED,
        action="authenticate",
        outcome=AuditOutcome.FAILURE,
        details={"password": SECRET_VALUE, "reason": "invalid_password"},
    )
    assert entry_id is not None

    stored = app_session.get(AuditLog, entry_id)
    assert stored is not None
    assert stored.details == {"password": REDACTED, "reason": "invalid_password"}
    assert SECRET_VALUE not in str(stored.details)


# --- append-only surface ------------------------------------------------------


def test_the_service_exposes_no_way_to_change_history() -> None:
    surface = {name for name in dir(AuditService) if not name.startswith("_")}
    forbidden = {
        name
        for name in surface
        if any(verb in name.lower() for verb in ("update", "delete", "remove", "purge"))
    }
    assert forbidden == set(), f"audit service must stay append-only, found: {forbidden}"


def test_the_repository_exposes_no_way_to_change_history() -> None:
    surface = {name for name in dir(AuditRepository) if not name.startswith("_")}
    forbidden = {
        name
        for name in surface
        if any(verb in name.lower() for verb in ("update", "delete", "remove", "purge"))
    }
    assert forbidden == set(), (
        f"audit repository must stay append-only, found: {forbidden}"
    )


# --- failure behaviour --------------------------------------------------------


def test_a_failed_write_is_logged_as_critical_and_never_raises(
    app_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    # event_type is a 64-character column; a longer value cannot be stored.
    service = AuditService(app_session)
    with caplog.at_level(logging.CRITICAL):
        result = service.record_security(
            event_type="X" * 200,
            action="oversized event type",
            outcome=AuditOutcome.FAILURE,
        )

    assert result is None
    assert any(record.levelno == logging.CRITICAL for record in caplog.records)
    assert "audit entry could not be written" in caplog.text


def test_a_failed_audit_write_leaves_the_transaction_usable(
    app_session: Session,
) -> None:
    # The write happens in a savepoint on purpose: losing an audit row is bad,
    # but taking the whole request down with it would be worse.
    AuditService(app_session).record_security(
        event_type="X" * 200,
        action="oversized event type",
        outcome=AuditOutcome.FAILURE,
    )

    user = User(
        email=f"after-failure-{uuid.uuid4().hex[:8]}@example.cl",
        password_hash="$argon2id$placeholder",
        full_name="Still Working",
    )
    app_session.add(user)
    app_session.flush()
    assert app_session.get(User, user.id) is not None


# --- event catalogue ----------------------------------------------------------


def test_event_names_are_unique() -> None:
    names = [
        value
        for name, value in vars(events).items()
        if name.isupper() and isinstance(value, str)
    ]
    assert len(names) == len(set(names))


def test_phase_one_events_are_all_defined() -> None:
    expected = {
        "USER_REGISTERED",
        "LOGIN_SUCCESS",
        "LOGIN_FAILED",
        "LOGIN_2FA_REQUIRED",
        "LOGOUT",
        "ACCOUNT_LOCKED",
        "PASSWORD_REHASHED",
        "PASSWORD_CHANGED",
        "2FA_ENROLLMENT_STARTED",
        "2FA_ENABLED",
        "2FA_DISABLED",
        "2FA_VERIFIED",
        "2FA_FAILED",
        "2FA_LOCKED",
        "2FA_RECOVERY_CODE_USED",
        "TOKEN_REFRESHED",
        "REFRESH_TOKEN_REUSE_DETECTED",
        "AUTHORIZATION_DENIED",
        "ROLE_GRANTED",
        "ROLE_REVOKED",
        "ROLE_SWITCHED",
    }
    defined = {
        value
        for name, value in vars(events).items()
        if name.isupper() and isinstance(value, str)
    }
    assert expected <= defined, f"missing events: {expected - defined}"


def test_an_entry_records_who_what_when_and_with_what_outcome(
    app_session: Session,
) -> None:
    actor = uuid.uuid4()
    entry_id = AuditService(app_session).record(
        event_category=AuditEventCategory.DOMAIN,
        event_type=events.ROLE_GRANTED,
        severity=AuditEventSeverity.WARNING,
        action="grant role",
        outcome=AuditOutcome.SUCCESS,
        actor_user_id=actor,
        actor_ip="192.0.2.10",
        target_resource_type="user",
        target_resource_id=actor,
        correlation_id=uuid.uuid4(),
    )

    stored = app_session.execute(
        select(AuditLog).where(AuditLog.id == entry_id)
    ).scalar_one()
    assert stored.event_category is AuditEventCategory.DOMAIN
    assert stored.severity is AuditEventSeverity.WARNING
    assert stored.outcome is AuditOutcome.SUCCESS
    assert stored.actor_user_id == actor
    assert stored.actor_ip == "192.0.2.10"
    assert stored.timestamp is not None
    assert stored.timestamp.tzinfo is not None
    # Reserved for the Phase 7 hash chain; nothing writes it yet.
    assert stored.hash_prev is None
