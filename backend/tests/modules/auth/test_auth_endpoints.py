"""Authentication endpoints: registration, login, 2FA, refresh and roles."""

from __future__ import annotations

import time
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pyotp
import pytest
import redis
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.audit import events
from app.modules.audit.models import AuditLog
from app.modules.auth import security as passwords
from app.modules.auth import two_factor
from app.modules.auth.models import RefreshToken, UserTwoFactor
from app.modules.users.models import User, UserRole
from app.shared.roles import ROLE_META, Role
from app.shared.security import create_access_token, create_challenge_token

API = "/api/v1/auth"
PASSWORD = "Cordillera-Sur-2026"  # noqa: S105 - test fixture, not a credential
OTHER_PASSWORD = "Precordillera-Norte-2027"  # noqa: S105 - test fixture


def _email() -> str:
    return f"user-{uuid.uuid4().hex[:10]}@example.cl"


def _make_user(
    session: Session,
    *,
    email: str | None = None,
    password: str = PASSWORD,
    roles: Sequence[Role] = (),
    is_active: bool = True,
) -> User:
    user = User(
        email=email or _email(),
        password_hash=passwords.hash_password(password),
        full_name="Ada Lovelace",
        is_active=is_active,
    )
    session.add(user)
    session.flush()
    for role in roles:
        session.add(UserRole(user_id=user.id, role=role))
    session.flush()
    return user


def _enable_two_factor(session: Session, user: User) -> str:
    """Enable 2FA for a user and return the plaintext secret."""
    secret = two_factor.generate_secret()
    session.add(
        UserTwoFactor(
            user_id=user.id,
            totp_secret_encrypted=two_factor.encrypt_secret(secret),
            totp_verified=True,
            enabled=True,
            recovery_codes_hash=[],
            failed_attempts=0,
        )
    )
    session.flush()
    return secret


def _audit_types(session: Session, user_id: uuid.UUID) -> list[str]:
    rows = session.execute(
        select(AuditLog.event_type).where(AuditLog.actor_user_id == user_id)
    ).scalars()
    return list(rows)


def _login(client: TestClient, email: str, password: str = PASSWORD) -> dict[str, object]:
    response = client.post(f"{API}/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return dict(response.json())


def _login_with_session(client: TestClient, email: str) -> dict[str, object]:
    """Log in and require a full session, not a 2FA challenge."""
    body = _login(client, email)
    assert body["access_token"], (
        "expected a session; the account got a 2FA challenge instead "
        "(roles in ROLES_2FA_REQUIRED never receive tokens from /login)"
    )
    return body


# --- registration -------------------------------------------------------------


def test_registration_creates_an_account_without_roles(
    api_client: TestClient, app_session: Session
) -> None:
    email = _email()
    response = api_client.post(
        f"{API}/register",
        json={"email": email, "password": PASSWORD, "full_name": "Ada Lovelace"},
    )
    assert response.status_code == 201, response.text

    user_id = uuid.UUID(response.json()["user_id"])
    user = app_session.get(User, user_id)
    assert user is not None
    assert user.email == email
    assert user.password_hash.startswith("$argon2id$")
    assert user.roles == []
    assert events.USER_REGISTERED in _audit_types(app_session, user_id)


def test_registration_does_not_return_a_session(api_client: TestClient) -> None:
    response = api_client.post(
        f"{API}/register",
        json={"email": _email(), "password": PASSWORD, "full_name": "Ada"},
    )
    body = response.json()
    assert "access_token" not in body
    assert "refresh_token" not in body


def test_duplicate_email_is_rejected(api_client: TestClient) -> None:
    email = _email()
    payload = {"email": email, "password": PASSWORD, "full_name": "Ada"}
    assert api_client.post(f"{API}/register", json=payload).status_code == 201
    assert api_client.post(f"{API}/register", json=payload).status_code == 409


def test_weak_password_is_rejected(api_client: TestClient) -> None:
    response = api_client.post(
        f"{API}/register",
        json={"email": _email(), "password": "corto", "full_name": "Ada"},
    )
    assert response.status_code == 422


def test_common_password_is_rejected(api_client: TestClient) -> None:
    response = api_client.post(
        f"{API}/register",
        json={"email": _email(), "password": "Password1234", "full_name": "Ada"},
    )
    assert response.status_code == 422


def test_invalid_rut_is_rejected(api_client: TestClient) -> None:
    response = api_client.post(
        f"{API}/register",
        json={
            "email": _email(),
            "password": PASSWORD,
            "full_name": "Ada",
            "rut": "12.345.678-9",
        },
    )
    assert response.status_code == 422


# --- login --------------------------------------------------------------------


def test_login_without_two_factor_issues_tokens(
    api_client: TestClient, app_session: Session
) -> None:
    user = _make_user(app_session, roles=[Role.APLICADOR])
    body = _login(api_client, user.email)

    assert body["requires_2fa"] is False
    assert body["access_token"] and body["refresh_token"]
    assert events.LOGIN_SUCCESS in _audit_types(app_session, user.id)


def test_wrong_password_is_rejected(api_client: TestClient, app_session: Session) -> None:
    user = _make_user(app_session)
    response = api_client.post(
        f"{API}/login", json={"email": user.email, "password": OTHER_PASSWORD}
    )
    assert response.status_code == 401
    assert events.LOGIN_FAILED in _audit_types(app_session, user.id)


def test_unknown_account_is_indistinguishable_from_a_wrong_password(
    api_client: TestClient, app_session: Session
) -> None:
    user = _make_user(app_session)

    wrong_password = api_client.post(
        f"{API}/login", json={"email": user.email, "password": OTHER_PASSWORD}
    )
    unknown_email = api_client.post(
        f"{API}/login", json={"email": _email(), "password": OTHER_PASSWORD}
    )

    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json()


def test_unknown_account_costs_the_same_time_as_a_wrong_password(
    api_client: TestClient, app_session: Session
) -> None:
    # Without the dummy verification, a missing account answers almost
    # instantly and the response time enumerates registered emails.
    user = _make_user(app_session)

    started = time.perf_counter()
    api_client.post(
        f"{API}/login", json={"email": user.email, "password": OTHER_PASSWORD}
    )
    known = time.perf_counter() - started

    started = time.perf_counter()
    api_client.post(f"{API}/login", json={"email": _email(), "password": OTHER_PASSWORD})
    unknown = time.perf_counter() - started

    assert unknown > known * 0.5, f"unknown={unknown:.3f}s vs known={known:.3f}s"


def test_five_failures_lock_the_account(
    api_client: TestClient, app_session: Session
) -> None:
    user = _make_user(app_session)
    for _ in range(5):
        api_client.post(
            f"{API}/login", json={"email": user.email, "password": OTHER_PASSWORD}
        )

    locked = api_client.post(
        f"{API}/login", json={"email": user.email, "password": PASSWORD}
    )
    assert locked.status_code == 423
    assert events.ACCOUNT_LOCKED in _audit_types(app_session, user.id)


def test_an_inactive_account_cannot_log_in(
    api_client: TestClient, app_session: Session
) -> None:
    user = _make_user(app_session, is_active=False)
    response = api_client.post(
        f"{API}/login", json={"email": user.email, "password": PASSWORD}
    )
    assert response.status_code == 401


def test_login_rehashes_an_outdated_hash(
    api_client: TestClient, app_session: Session
) -> None:
    from argon2 import PasswordHasher

    from app.shared.config import get_settings

    settings = get_settings()
    weaker = PasswordHasher(
        time_cost=max(1, settings.argon2_time_cost - 1),
        memory_cost=settings.argon2_memory_cost // 2,
        parallelism=settings.argon2_parallelism,
        hash_len=32,
        salt_len=16,
    )
    user = _make_user(app_session)
    user.password_hash = weaker.hash(PASSWORD)
    app_session.flush()
    legacy_hash = user.password_hash

    _login(api_client, user.email)
    app_session.refresh(user)

    assert user.password_hash != legacy_hash
    assert passwords.needs_rehash(user.password_hash) is False
    assert events.PASSWORD_REHASHED in _audit_types(app_session, user.id)


# --- two-factor ---------------------------------------------------------------


def test_login_with_two_factor_returns_a_challenge_instead_of_tokens(
    api_client: TestClient, app_session: Session
) -> None:
    user = _make_user(app_session, roles=[Role.APLICADOR])
    _enable_two_factor(app_session, user)

    body = _login(api_client, user.email)
    assert body["requires_2fa"] is True
    assert body["challenge_token"]
    assert body["access_token"] is None
    assert events.LOGIN_2FA_REQUIRED in _audit_types(app_session, user.id)


def test_a_role_that_requires_two_factor_forces_enrollment(
    api_client: TestClient, app_session: Session
) -> None:
    user = _make_user(app_session, roles=[Role.AUDITOR])
    body = _login(api_client, user.email)

    assert body["requires_2fa_enrollment"] is True
    assert body["access_token"] is None


def test_valid_code_completes_the_login(
    api_client: TestClient, app_session: Session, redis_client: redis.Redis
) -> None:
    user = _make_user(app_session, roles=[Role.APLICADOR])
    secret = _enable_two_factor(app_session, user)
    challenge = _login(api_client, user.email)["challenge_token"]

    response = api_client.post(
        f"{API}/2fa/verify",
        json={"challenge_token": challenge, "code": pyotp.TOTP(secret).now()},
    )
    assert response.status_code == 200, response.text
    assert response.json()["access_token"]
    assert events.TWO_FACTOR_VERIFIED in _audit_types(app_session, user.id)


def test_invalid_code_is_rejected(
    api_client: TestClient, app_session: Session, redis_client: redis.Redis
) -> None:
    user = _make_user(app_session, roles=[Role.APLICADOR])
    _enable_two_factor(app_session, user)
    challenge = _login(api_client, user.email)["challenge_token"]

    response = api_client.post(
        f"{API}/2fa/verify", json={"challenge_token": challenge, "code": "000000"}
    )
    assert response.status_code == 401
    assert events.TWO_FACTOR_FAILED in _audit_types(app_session, user.id)


def test_a_recovery_code_completes_the_login_once(
    api_client: TestClient, app_session: Session, redis_client: redis.Redis
) -> None:
    user = _make_user(app_session, roles=[Role.APLICADOR])
    _enable_two_factor(app_session, user)
    plain, hashes = two_factor.generate_recovery_codes()
    enrolment = app_session.get(UserTwoFactor, user.id)
    assert enrolment is not None
    enrolment.recovery_codes_hash = hashes
    app_session.flush()

    challenge = _login(api_client, user.email)["challenge_token"]
    first = api_client.post(
        f"{API}/2fa/verify", json={"challenge_token": challenge, "code": plain[0]}
    )
    assert first.status_code == 200, first.text

    challenge = _login(api_client, user.email)["challenge_token"]
    second = api_client.post(
        f"{API}/2fa/verify", json={"challenge_token": challenge, "code": plain[0]}
    )
    assert second.status_code == 401
    assert events.TWO_FACTOR_RECOVERY_CODE_USED in _audit_types(app_session, user.id)


def test_enrollment_flow_returns_recovery_codes_and_enables_two_factor(
    api_client: TestClient, app_session: Session, redis_client: redis.Redis
) -> None:
    user = _make_user(app_session, roles=[Role.APLICADOR])
    token = _login_with_session(api_client, user.email)["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    enroll = api_client.post(f"{API}/2fa/enroll", headers=headers)
    assert enroll.status_code == 200, enroll.text
    secret = enroll.json()["secret"]
    assert enroll.json()["qr_code"].startswith("data:image/png;base64,")

    confirm = api_client.post(
        f"{API}/2fa/enroll/confirm",
        headers=headers,
        json={"code": pyotp.TOTP(secret).now()},
    )
    assert confirm.status_code == 200, confirm.text
    assert len(confirm.json()["recovery_codes"]) == 10

    enrolment = app_session.get(UserTwoFactor, user.id)
    assert enrolment is not None
    assert enrolment.enabled is True
    assert events.TWO_FACTOR_ENABLED in _audit_types(app_session, user.id)


def test_two_factor_cannot_be_disabled_for_a_role_that_requires_it(
    api_client: TestClient, app_session: Session, redis_client: redis.Redis
) -> None:
    user = _make_user(app_session, roles=[Role.AUDITOR])
    secret = _enable_two_factor(app_session, user)
    challenge = _login(api_client, user.email)["challenge_token"]
    tokens = api_client.post(
        f"{API}/2fa/verify",
        json={"challenge_token": challenge, "code": pyotp.TOTP(secret).now()},
    ).json()

    response = api_client.post(
        f"{API}/2fa/disable",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
        json={"code": pyotp.TOTP(secret).now()},
    )
    assert response.status_code == 403


# --- token separation ---------------------------------------------------------


def test_a_challenge_token_is_not_a_session(
    api_client: TestClient, app_session: Session
) -> None:
    user = _make_user(app_session, roles=[Role.APLICADOR])
    challenge = create_challenge_token(user.id, timedelta(minutes=5))

    response = api_client.get(
        f"{API}/me", headers={"Authorization": f"Bearer {challenge}"}
    )
    assert response.status_code == 401


def test_an_access_token_is_not_a_challenge(
    api_client: TestClient, app_session: Session
) -> None:
    user = _make_user(app_session, roles=[Role.APLICADOR])
    _enable_two_factor(app_session, user)
    access = create_access_token(user.id, [Role.APLICADOR], Role.APLICADOR)

    response = api_client.post(
        f"{API}/2fa/verify", json={"challenge_token": access, "code": "000000"}
    )
    assert response.status_code == 401


def test_a_protected_endpoint_needs_a_token(api_client: TestClient) -> None:
    assert api_client.get(f"{API}/me").status_code == 401


@pytest.mark.parametrize(
    "header",
    ["", "Bearer ", "Bearer not-a-token", "Basic dXNlcjpwYXNz"],
)
def test_malformed_authorization_headers_are_rejected(
    api_client: TestClient, header: str
) -> None:
    response = api_client.get(f"{API}/me", headers={"Authorization": header})
    assert response.status_code == 401


def test_an_expired_token_is_rejected(
    api_client: TestClient, app_session: Session
) -> None:
    user = _make_user(app_session, roles=[Role.APLICADOR])
    expired = create_access_token(
        user.id, [Role.APLICADOR], Role.APLICADOR, expires_delta=timedelta(minutes=-60)
    )
    response = api_client.get(f"{API}/me", headers={"Authorization": f"Bearer {expired}"})
    assert response.status_code == 401


# --- sessions -----------------------------------------------------------------


def test_refresh_rotates_the_token(api_client: TestClient, app_session: Session) -> None:
    user = _make_user(app_session, roles=[Role.APLICADOR])
    original = _login_with_session(api_client, user.email)["refresh_token"]

    response = api_client.post(f"{API}/refresh", json={"refresh_token": original})
    assert response.status_code == 200, response.text
    assert response.json()["refresh_token"] != original
    assert events.TOKEN_REFRESHED in _audit_types(app_session, user.id)


def test_reusing_a_spent_refresh_token_burns_every_session(
    api_client: TestClient, app_session: Session
) -> None:
    user = _make_user(app_session, roles=[Role.APLICADOR])
    original = _login_with_session(api_client, user.email)["refresh_token"]
    rotated = api_client.post(f"{API}/refresh", json={"refresh_token": original}).json()[
        "refresh_token"
    ]

    replay = api_client.post(f"{API}/refresh", json={"refresh_token": original})
    assert replay.status_code == 401
    assert events.REFRESH_TOKEN_REUSE_DETECTED in _audit_types(app_session, user.id)

    # The token issued by the rotation is burned as well.
    assert (
        api_client.post(f"{API}/refresh", json={"refresh_token": rotated}).status_code
        == 401
    )

    active = app_session.execute(
        select(RefreshToken).where(
            RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None)
        )
    ).scalars()
    assert list(active) == []


def test_an_unknown_refresh_token_is_rejected(api_client: TestClient) -> None:
    response = api_client.post(f"{API}/refresh", json={"refresh_token": "made-up"})
    assert response.status_code == 401


def test_logout_revokes_the_refresh_token(
    api_client: TestClient, app_session: Session
) -> None:
    user = _make_user(app_session, roles=[Role.APLICADOR])
    body = _login_with_session(api_client, user.email)
    headers = {"Authorization": f"Bearer {body['access_token']}"}

    logout = api_client.post(
        f"{API}/logout", headers=headers, json={"refresh_token": body["refresh_token"]}
    )
    assert logout.status_code == 204

    reuse = api_client.post(
        f"{API}/refresh", json={"refresh_token": body["refresh_token"]}
    )
    assert reuse.status_code == 401
    assert events.LOGOUT in _audit_types(app_session, user.id)


# --- identity and roles -------------------------------------------------------


def test_me_reports_roles_with_both_labels(
    api_client: TestClient, app_session: Session
) -> None:
    # Operador de Drone is deliberately a role whose label and short label
    # differ ("Operador de Drone" / "Operador"), and which does not force 2FA.
    user = _make_user(app_session, roles=[Role.OPERADOR_DRONE])
    token = _login_with_session(api_client, user.email)["access_token"]

    response = api_client.get(f"{API}/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["user"]["email"] == user.email
    assert body["active_role"] == Role.OPERADOR_DRONE.value
    granted = body["roles"][0]
    assert granted["label"] == ROLE_META[Role.OPERADOR_DRONE].label
    assert granted["short_label"] == ROLE_META[Role.OPERADOR_DRONE].short_label
    assert granted["label"] != granted["short_label"]
    assert body["two_factor_enabled"] is False


def test_me_never_exposes_the_password_hash(
    api_client: TestClient, app_session: Session
) -> None:
    user = _make_user(app_session, roles=[Role.APLICADOR])
    token = _login_with_session(api_client, user.email)["access_token"]
    response = api_client.get(f"{API}/me", headers={"Authorization": f"Bearer {token}"})
    assert "password" not in response.text.lower()


def test_switching_to_a_granted_role_reissues_the_token(
    api_client: TestClient, app_session: Session
) -> None:
    user = _make_user(app_session, roles=[Role.BODEGUERO, Role.OPERADOR_DRONE])
    token = _login_with_session(api_client, user.email)["access_token"]

    response = api_client.post(
        f"{API}/switch-role",
        headers={"Authorization": f"Bearer {token}"},
        json={"role": Role.BODEGUERO.value},
    )
    assert response.status_code == 200, response.text
    assert response.json()["active_role"] == Role.BODEGUERO.value
    assert events.ROLE_SWITCHED in _audit_types(app_session, user.id)


def test_switching_to_a_role_the_user_lacks_is_denied(
    api_client: TestClient, app_session: Session
) -> None:
    user = _make_user(app_session, roles=[Role.BODEGUERO])
    token = _login_with_session(api_client, user.email)["access_token"]

    response = api_client.post(
        f"{API}/switch-role",
        headers={"Authorization": f"Bearer {token}"},
        json={"role": Role.PROPIETARIO.value},
    )
    assert response.status_code == 403
    assert events.AUTHORIZATION_DENIED in _audit_types(app_session, user.id)


def test_switching_to_an_expired_role_is_denied(
    api_client: TestClient, app_session: Session
) -> None:
    user = _make_user(app_session, roles=[Role.BODEGUERO])
    app_session.add(
        UserRole(
            user_id=user.id,
            role=Role.JEFE_BODEGA,
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
        )
    )
    app_session.flush()
    token = _login_with_session(api_client, user.email)["access_token"]

    response = api_client.post(
        f"{API}/switch-role",
        headers={"Authorization": f"Bearer {token}"},
        json={"role": Role.JEFE_BODEGA.value},
    )
    assert response.status_code == 403
