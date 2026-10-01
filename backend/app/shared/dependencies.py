"""Shared FastAPI dependencies.

Every dependency here fails closed: when identity or authority cannot be
established beyond doubt, the request is rejected rather than downgraded.
"""

from __future__ import annotations

import ipaddress
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.modules.audit.events import AUTHORIZATION_DENIED
from app.modules.audit.service import AuditService
from app.modules.users.models import User
from app.modules.users.repository import UserRepository, UserRoleRepository
from app.shared.db import get_session
from app.shared.enums import AuditEventSeverity, AuditOutcome
from app.shared.permissions import Permission, can_access_predio, has_permission
from app.shared.roles import Role
from app.shared.security import PURPOSE_ACCESS, InvalidTokenError, decode_token

bearer_scheme = HTTPBearer(auto_error=False)

# One message for every authentication failure: a caller must not be able to
# tell a missing token from an expired one, or a valid one for a locked user.
_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


@dataclass(frozen=True)
class CurrentUser:
    """The authenticated caller, as established by the access token."""

    id: uuid.UUID
    email: str
    roles: frozenset[Role]
    active_role: Role | None
    predio_ids: frozenset[uuid.UUID]
    two_factor_verified: bool


def client_ip(request: Request) -> str | None:
    """Return the caller's IP address, when it is one.

    The audit column is INET, so anything that is not a parseable address
    (a test client's "testclient", a hostname from an odd proxy) is dropped
    rather than allowed to break the insert.
    """
    if request.client is None:
        return None
    try:
        return str(ipaddress.ip_address(request.client.host))
    except ValueError:
        return None


def user_agent(request: Request) -> str | None:
    """Return the caller's User-Agent header, truncated to the column width."""
    value = request.headers.get("user-agent")
    return value[:512] if value else None


def _is_usable(user: User | None) -> bool:
    if user is None or not user.is_active:
        return False
    # A locked account holds no session, even with a still-valid token.
    return not (user.locked_until is not None and user.locked_until > datetime.now(UTC))


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    session: Annotated[Session, Depends(get_session)],
) -> CurrentUser:
    """Resolve the caller from the bearer token, or reject the request."""
    if credentials is None or not credentials.credentials.strip():
        raise _UNAUTHORIZED

    try:
        payload = decode_token(credentials.credentials)
    except InvalidTokenError as exc:
        raise _UNAUTHORIZED from exc

    # A challenge token proves the password step only; it is not a session.
    if payload.purpose != PURPOSE_ACCESS:
        raise _UNAUTHORIZED

    user = UserRepository(session).get_by_id(payload.sub)
    if user is None or not _is_usable(user):
        raise _UNAUTHORIZED

    return CurrentUser(
        id=user.id,
        email=user.email,
        roles=frozenset(payload.roles),
        active_role=payload.active_role,
        predio_ids=frozenset(payload.predio_ids),
        two_factor_verified=payload.two_factor_verified,
    )


def get_current_user_optional(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    session: Annotated[Session, Depends(get_session)],
) -> CurrentUser | None:
    """Resolve the caller when a valid token is present, otherwise None."""
    if credentials is None:
        return None
    try:
        return get_current_user(credentials, session)
    except HTTPException:
        return None


def _deny(
    request: Request,
    session: Session,
    user: CurrentUser,
    *,
    action: str,
    detail: str,
    predio_id: uuid.UUID | None = None,
) -> HTTPException:
    """Record the denial and build the 403 to raise."""
    AuditService(session).record_security(
        event_type=AUTHORIZATION_DENIED,
        action=action,
        outcome=AuditOutcome.DENIED,
        severity=AuditEventSeverity.WARNING,
        actor_user_id=user.id,
        actor_role=user.active_role,
        actor_ip=client_ip(request),
        actor_user_agent=user_agent(request),
        predio_id=predio_id,
        details={"path": request.url.path, "method": request.method},
    )
    session.commit()
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def require_role(*roles: Role) -> Callable[..., CurrentUser]:
    """Dependency factory: the caller must hold one of ``roles``."""

    def dependency(
        request: Request,
        user: Annotated[CurrentUser, Depends(get_current_user)],
        session: Annotated[Session, Depends(get_session)],
    ) -> CurrentUser:
        if user.roles.isdisjoint(roles):
            raise _deny(
                request,
                session,
                user,
                action=f"require_role:{','.join(role.value for role in roles)}",
                detail="Insufficient role",
            )
        return user

    return dependency


def require_permission(permission: Permission) -> Callable[..., CurrentUser]:
    """Dependency factory: the caller's roles must grant ``permission``."""

    def dependency(
        request: Request,
        user: Annotated[CurrentUser, Depends(get_current_user)],
        session: Annotated[Session, Depends(get_session)],
    ) -> CurrentUser:
        if not has_permission(user.roles, permission):
            raise _deny(
                request,
                session,
                user,
                action=f"require_permission:{permission.value}",
                detail="Insufficient permissions",
            )
        return user

    return dependency


def require_predio_access(permission: Permission) -> Callable[..., CurrentUser]:
    """Dependency factory: ``permission`` must hold on the requested predio.

    The predio id is read from the path or the query string, so changing it in
    the URL is checked rather than trusted.
    """

    def dependency(
        request: Request,
        user: Annotated[CurrentUser, Depends(get_current_user)],
        session: Annotated[Session, Depends(get_session)],
    ) -> CurrentUser:
        raw = request.path_params.get("predio_id") or request.query_params.get(
            "predio_id"
        )
        try:
            predio_id = uuid.UUID(str(raw))
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid predio id"
            ) from exc

        # Both sides come from the database. The token's role list is not a
        # substitute: it mixes global and predio-scoped grants, and treating it
        # as global would grant access to every predio the role appears on.
        repository = UserRoleRepository(session)
        predio_grants = list(repository.list_predio_roles(user.id))
        global_roles = [grant.role for grant in repository.list_global_roles(user.id)]
        if not can_access_predio(
            predio_grants, predio_id, permission, global_roles=global_roles
        ):
            raise _deny(
                request,
                session,
                user,
                action=f"require_predio_access:{permission.value}",
                detail="Insufficient permissions on this predio",
                predio_id=predio_id,
            )
        return user

    return dependency
