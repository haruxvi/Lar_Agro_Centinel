"""Authentication and authorization dependencies."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Annotated, Any

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.audit.events import AUTHORIZATION_DENIED
from app.modules.audit.models import AuditLog
from app.modules.predios.models import Predio
from app.modules.users.models import User, UserPredioRole, UserRole
from app.shared.config import get_settings
from app.shared.db import get_session
from app.shared.dependencies import (
    CurrentUser,
    get_current_user,
    get_current_user_optional,
    require_permission,
    require_predio_access,
    require_role,
)
from app.shared.permissions import Permission
from app.shared.roles import Role
from app.shared.security import (
    AUDIENCE,
    create_access_token,
    create_challenge_token,
)
from tests.fixtures.predios import create_predio

PREDIO_MINE = uuid.UUID("11111111-2222-3333-4444-555555555555")
PREDIO_THEIRS = uuid.UUID("99999999-8888-7777-6666-555555555555")


@pytest.fixture
def protected_client(app_session: Session) -> Iterator[TestClient]:
    """Return a minimal app whose routes exercise each dependency in isolation."""
    app = FastAPI()
    app.dependency_overrides[get_session] = lambda: app_session

    @app.get("/needs-permission")
    def needs_permission(
        user: Annotated[
            CurrentUser, Depends(require_permission(Permission.MISSION_PLAN))
        ],
    ) -> dict[str, str]:
        return {"user": str(user.id)}

    @app.get("/needs-role")
    def needs_role(
        user: Annotated[CurrentUser, Depends(require_role(Role.AUDITOR))],
    ) -> dict[str, str]:
        return {"user": str(user.id)}

    @app.get("/predios/{predio_id}/apply")
    def needs_predio(
        predio_id: uuid.UUID,
        user: Annotated[
            CurrentUser, Depends(require_predio_access(Permission.APPLICATION_EXECUTE))
        ],
    ) -> dict[str, str]:
        return {"predio": str(predio_id)}

    @app.get("/predios/{predio_id}/audit")
    def needs_predio_audit(
        predio_id: uuid.UUID,
        user: Annotated[
            CurrentUser, Depends(require_predio_access(Permission.AUDIT_VIEW_PREDIO))
        ],
    ) -> dict[str, str]:
        return {"predio": str(predio_id)}

    @app.get("/whoami")
    def whoami(
        user: Annotated[CurrentUser, Depends(get_current_user)],
    ) -> dict[str, Any]:
        return {"id": str(user.id), "active_role": user.active_role}

    @app.get("/maybe")
    def maybe(
        user: Annotated[CurrentUser | None, Depends(get_current_user_optional)],
    ) -> dict[str, Any]:
        return {"authenticated": user is not None}

    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def _user(session: Session, *, is_active: bool = True) -> User:
    user = User(
        email=f"dep-{uuid.uuid4().hex[:8]}@example.cl",
        password_hash="$argon2id$placeholder",
        full_name="Dependency Subject",
        is_active=is_active,
    )
    session.add(user)
    session.flush()
    return user


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _denials(session: Session, user_id: uuid.UUID) -> list[AuditLog]:
    rows = session.execute(
        select(AuditLog).where(
            AuditLog.actor_user_id == user_id,
            AuditLog.event_type == AUTHORIZATION_DENIED,
        )
    ).scalars()
    return list(rows)


# --- authentication -----------------------------------------------------------


def test_a_valid_token_identifies_the_caller(
    protected_client: TestClient, app_session: Session
) -> None:
    user = _user(app_session)
    token = create_access_token(user.id, [Role.AGRONOMO], Role.AGRONOMO)

    response = protected_client.get("/whoami", headers=_auth(token))
    assert response.status_code == 200
    assert response.json() == {"id": str(user.id), "active_role": Role.AGRONOMO.value}


def test_no_token_is_rejected(protected_client: TestClient) -> None:
    assert protected_client.get("/whoami").status_code == 401


@pytest.mark.parametrize("header", ["Bearer", "Bearer ", "Bearer    ", "token abc"])
def test_empty_or_malformed_bearer_headers_are_rejected(
    protected_client: TestClient, header: str
) -> None:
    response = protected_client.get("/whoami", headers={"Authorization": header})
    assert response.status_code == 401


def test_a_token_from_another_issuer_is_rejected(
    protected_client: TestClient, app_session: Session
) -> None:
    settings = get_settings()
    user = _user(app_session)
    foreign = jwt.encode(
        {
            "sub": str(user.id),
            "iss": "another-system",
            "aud": AUDIENCE,
            "iat": 1,
            "exp": 9_999_999_999,
            "jti": str(uuid.uuid4()),
            "roles": [Role.AGRONOMO.value],
            "active_role": Role.AGRONOMO.value,
            "purpose": "access",
        },
        settings.secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )
    assert protected_client.get("/whoami", headers=_auth(foreign)).status_code == 401


def test_a_challenge_token_is_rejected_where_a_session_is_required(
    protected_client: TestClient, app_session: Session
) -> None:
    user = _user(app_session)
    challenge = create_challenge_token(user.id, timedelta(minutes=5))
    assert protected_client.get("/whoami", headers=_auth(challenge)).status_code == 401


def test_a_deactivated_user_holds_no_session(
    protected_client: TestClient, app_session: Session
) -> None:
    # The token is still cryptographically valid; the account is not.
    user = _user(app_session, is_active=False)
    token = create_access_token(user.id, [Role.AGRONOMO], Role.AGRONOMO)
    assert protected_client.get("/whoami", headers=_auth(token)).status_code == 401


def test_the_optional_dependency_allows_anonymous_callers(
    protected_client: TestClient, app_session: Session
) -> None:
    assert protected_client.get("/maybe").json() == {"authenticated": False}

    user = _user(app_session)
    token = create_access_token(user.id, [Role.AGRONOMO], Role.AGRONOMO)
    assert protected_client.get("/maybe", headers=_auth(token)).json() == {
        "authenticated": True
    }


def test_the_optional_dependency_still_rejects_a_bad_token(
    protected_client: TestClient,
) -> None:
    assert protected_client.get("/maybe", headers=_auth("garbage")).json() == {
        "authenticated": False
    }


# --- authorization ------------------------------------------------------------


def test_a_role_with_the_permission_is_allowed(
    protected_client: TestClient, app_session: Session
) -> None:
    user = _user(app_session)
    token = create_access_token(user.id, [Role.ADMIN_OPERACIONES], Role.ADMIN_OPERACIONES)
    assert (
        protected_client.get("/needs-permission", headers=_auth(token)).status_code == 200
    )


def test_a_role_without_the_permission_is_denied_and_recorded(
    protected_client: TestClient, app_session: Session
) -> None:
    user = _user(app_session)
    token = create_access_token(user.id, [Role.APLICADOR], Role.APLICADOR)

    response = protected_client.get("/needs-permission", headers=_auth(token))
    assert response.status_code == 403

    denials = _denials(app_session, user.id)
    assert len(denials) == 1
    assert denials[0].details["path"] == "/needs-permission"
    assert denials[0].outcome.value == "DENIED"


def test_require_role_checks_the_role_itself(
    protected_client: TestClient, app_session: Session
) -> None:
    user = _user(app_session)
    auditor = create_access_token(user.id, [Role.AUDITOR], Role.AUDITOR)
    bodeguero = create_access_token(user.id, [Role.BODEGUERO], Role.BODEGUERO)

    assert protected_client.get("/needs-role", headers=_auth(auditor)).status_code == 200
    assert (
        protected_client.get("/needs-role", headers=_auth(bodeguero)).status_code == 403
    )


# --- predio scoping -----------------------------------------------------------


def _grant_predio(session: Session, user: User, predio_id: uuid.UUID, role: Role) -> None:
    if session.get(Predio, predio_id) is None:
        create_predio(session, user.id, predio_id=predio_id)
    session.add(UserPredioRole(user_id=user.id, predio_id=predio_id, role=role))
    session.flush()


def test_access_to_an_assigned_predio_is_allowed(
    protected_client: TestClient, app_session: Session
) -> None:
    user = _user(app_session)
    _grant_predio(app_session, user, PREDIO_MINE, Role.APLICADOR)
    token = create_access_token(user.id, [Role.APLICADOR], Role.APLICADOR, [PREDIO_MINE])

    response = protected_client.get(f"/predios/{PREDIO_MINE}/apply", headers=_auth(token))
    assert response.status_code == 200


def test_changing_the_predio_id_in_the_url_does_not_grant_access(
    protected_client: TestClient, app_session: Session
) -> None:
    # IDOR: same user, same role, a predio they hold no grant on.
    user = _user(app_session)
    _grant_predio(app_session, user, PREDIO_MINE, Role.APLICADOR)
    token = create_access_token(user.id, [Role.APLICADOR], Role.APLICADOR, [PREDIO_MINE])

    response = protected_client.get(
        f"/predios/{PREDIO_THEIRS}/apply", headers=_auth(token)
    )
    assert response.status_code == 403

    denials = _denials(app_session, user.id)
    assert len(denials) == 1
    assert denials[0].predio_id == PREDIO_THEIRS


def test_a_token_claiming_a_predio_without_a_grant_is_not_enough(
    protected_client: TestClient, app_session: Session
) -> None:
    # The grant is read from the database, never taken from the token's word.
    user = _user(app_session)
    token = create_access_token(
        user.id, [Role.APLICADOR], Role.APLICADOR, [PREDIO_THEIRS]
    )
    response = protected_client.get(
        f"/predios/{PREDIO_THEIRS}/apply", headers=_auth(token)
    )
    assert response.status_code == 403


def test_a_global_role_reaches_predios_it_holds_no_grant_on(
    protected_client: TestClient, app_session: Session
) -> None:
    # A system-wide auditor is granted the role globally, not per predio, and
    # must still be able to read any of them.
    user = _user(app_session)
    app_session.add(UserRole(user_id=user.id, role=Role.AUDITOR))
    app_session.flush()
    token = create_access_token(user.id, [Role.AUDITOR], Role.AUDITOR)

    response = protected_client.get(
        f"/predios/{PREDIO_THEIRS}/audit", headers=_auth(token)
    )
    assert response.status_code == 200


def test_a_predio_scoped_role_does_not_become_global(
    protected_client: TestClient, app_session: Session
) -> None:
    # The same role, granted on one predio only, must not unlock the others.
    user = _user(app_session)
    _grant_predio(app_session, user, PREDIO_MINE, Role.AUDITOR)
    token = create_access_token(user.id, [Role.AUDITOR], Role.AUDITOR, [PREDIO_MINE])

    assert (
        protected_client.get(
            f"/predios/{PREDIO_MINE}/audit", headers=_auth(token)
        ).status_code
        == 200
    )
    assert (
        protected_client.get(
            f"/predios/{PREDIO_THEIRS}/audit", headers=_auth(token)
        ).status_code
        == 403
    )
