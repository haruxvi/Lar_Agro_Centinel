"""HTTP surface of the auth context."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from app.modules.auth.repository import TwoFactorRepository
from app.modules.auth.schema import (
    AccessTokenResponse,
    GrantedRole,
    LoginRequest,
    LoginResponse,
    MeResponse,
    RefreshRequest,
    RegisterRequest,
    RegisterResponse,
    StatusResponse,
    SwitchRoleRequest,
    TokenPair,
    TwoFactorCodeRequest,
    TwoFactorConfirmResponse,
    TwoFactorEnrollResponse,
    TwoFactorVerifyRequest,
)
from app.modules.auth.service import (
    AccountLockedError,
    AuthService,
    EmailAlreadyRegisteredError,
    InvalidCredentialsError,
    InvalidRefreshTokenError,
    InvalidTwoFactorCodeError,
    IssuedTokens,
    LoginStatus,
    RequestContext,
    RoleNotGrantedError,
    TwoFactorLockedError,
    TwoFactorNotEnrolledError,
    TwoFactorRequiredForRoleError,
)
from app.modules.users.repository import UserRepository, UserRoleRepository
from app.modules.users.schema import UserRead
from app.shared.db import get_session
from app.shared.dependencies import (
    CurrentUser,
    bearer_scheme,
    client_ip,
    get_current_user,
    user_agent,
)
from app.shared.rate_limit import (
    auth_limit,
    limiter,
    registration_limit,
    two_factor_limit,
)
from app.shared.roles import ROLE_META
from app.shared.security import PURPOSE_ACCESS, InvalidTokenError, decode_token

router = APIRouter(prefix="/auth", tags=["auth"])

SessionDep = Annotated[Session, Depends(get_session)]
CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]
CredentialsDep = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)]

# One wording for every credential failure: the caller must not be able to tell
# a wrong password from an unknown account.
_INVALID_CREDENTIALS = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials"
)


def _context(request: Request) -> RequestContext:
    return RequestContext(ip=client_ip(request), user_agent=user_agent(request))


def _token_pair(tokens: IssuedTokens) -> TokenPair:
    return TokenPair(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_in=tokens.expires_in,
    )


def get_enrolment_subject(credentials: CredentialsDep, session: SessionDep) -> uuid.UUID:
    """Resolve who is enrolling in 2FA.

    Accepts an access token (a user enrolling by choice) or a challenge token
    (a user whose role makes 2FA mandatory and who therefore cannot hold an
    access token yet). Nothing else is accepted.
    """
    if credentials is None or not credentials.credentials.strip():
        raise _INVALID_CREDENTIALS
    try:
        payload = decode_token(credentials.credentials)
    except InvalidTokenError as exc:
        raise _INVALID_CREDENTIALS from exc

    user = UserRepository(session).get_by_id(payload.sub)
    if user is None or not user.is_active:
        raise _INVALID_CREDENTIALS
    return payload.sub


@router.post("/register", status_code=status.HTTP_201_CREATED)
@limiter.limit(registration_limit)
def register(
    payload: RegisterRequest,
    request: Request,
    response: Response,
    session: SessionDep,
) -> RegisterResponse:
    """Create an account. No session is issued and no role is granted."""
    try:
        user = AuthService(session).register(
            email=payload.email,
            password=payload.password,
            full_name=payload.full_name,
            rut=payload.rut,
            phone=payload.phone,
            context=_context(request),
        )
    except EmailAlreadyRegisteredError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Email already registered"
        ) from exc
    return RegisterResponse(user_id=user.id)


@router.post("/login")
@limiter.limit(auth_limit)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: SessionDep,
) -> LoginResponse:
    """Verify credentials and either open a session or demand a second factor."""
    try:
        result = AuthService(session).authenticate(
            email=payload.email, password=payload.password, context=_context(request)
        )
    except AccountLockedError as exc:
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail="Account temporarily locked after repeated failed attempts",
        ) from exc
    except InvalidCredentialsError as exc:
        raise _INVALID_CREDENTIALS from exc

    if result.status is LoginStatus.TWO_FACTOR_REQUIRED:
        return LoginResponse(requires_2fa=True, challenge_token=result.challenge_token)
    if result.status is LoginStatus.TWO_FACTOR_ENROLLMENT_REQUIRED:
        return LoginResponse(
            requires_2fa_enrollment=True, challenge_token=result.challenge_token
        )

    assert result.tokens is not None  # noqa: S101 - guaranteed by LoginStatus
    return LoginResponse(
        access_token=result.tokens.access_token,
        refresh_token=result.tokens.refresh_token,
        expires_in=result.tokens.expires_in,
    )


@router.post("/2fa/verify")
@limiter.limit(two_factor_limit)
def verify_two_factor(
    payload: TwoFactorVerifyRequest,
    request: Request,
    response: Response,
    session: SessionDep,
) -> TokenPair:
    """Complete a login with a TOTP code or a recovery code."""
    try:
        tokens = AuthService(session).verify_two_factor(
            challenge_token=payload.challenge_token,
            code=payload.code,
            context=_context(request),
        )
    except TwoFactorLockedError as exc:
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail="Two-factor authentication temporarily locked",
        ) from exc
    except (
        InvalidTwoFactorCodeError,
        TwoFactorNotEnrolledError,
        InvalidCredentialsError,
    ) as exc:
        raise _INVALID_CREDENTIALS from exc
    return _token_pair(tokens)


@router.post("/2fa/enroll")
@limiter.limit(two_factor_limit)
def enroll_two_factor(
    request: Request,
    response: Response,
    session: SessionDep,
    user_id: Annotated[uuid.UUID, Depends(get_enrolment_subject)],
) -> TwoFactorEnrollResponse:
    """Start 2FA enrolment and return the secret, its URI and a QR code."""
    secret, uri, qr_code = AuthService(session).start_enrollment(
        user_id=user_id, context=_context(request)
    )
    return TwoFactorEnrollResponse(secret=secret, uri=uri, qr_code=qr_code)


@router.post("/2fa/enroll/confirm")
@limiter.limit(two_factor_limit)
def confirm_two_factor_enrollment(
    payload: TwoFactorCodeRequest,
    request: Request,
    response: Response,
    session: SessionDep,
    user_id: Annotated[uuid.UUID, Depends(get_enrolment_subject)],
) -> TwoFactorConfirmResponse:
    """Confirm enrolment with a live code and hand over the recovery codes."""
    try:
        codes = AuthService(session).confirm_enrollment(
            user_id=user_id, code=payload.code, context=_context(request)
        )
    except TwoFactorNotEnrolledError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Enrolment was not started"
        ) from exc
    except InvalidTwoFactorCodeError as exc:
        raise _INVALID_CREDENTIALS from exc
    return TwoFactorConfirmResponse(recovery_codes=codes)


@router.post("/2fa/disable")
@limiter.limit(two_factor_limit)
def disable_two_factor(
    payload: TwoFactorCodeRequest,
    request: Request,
    response: Response,
    session: SessionDep,
    current_user: CurrentUserDep,
) -> StatusResponse:
    """Disable 2FA. Refused when a role the caller holds requires it."""
    try:
        AuthService(session).disable_two_factor(
            user_id=current_user.id, code=payload.code, context=_context(request)
        )
    except TwoFactorRequiredForRoleError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Two-factor authentication is mandatory for one of your roles",
        ) from exc
    except TwoFactorNotEnrolledError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Two-factor is not enabled"
        ) from exc
    except InvalidTwoFactorCodeError as exc:
        raise _INVALID_CREDENTIALS from exc
    return StatusResponse(status="disabled")


@router.post("/refresh")
def refresh_session(
    payload: RefreshRequest, request: Request, session: SessionDep
) -> TokenPair:
    """Rotate a refresh token. Reusing a spent one burns every session."""
    try:
        tokens = AuthService(session).refresh(
            refresh_token=payload.refresh_token, context=_context(request)
        )
    except InvalidRefreshTokenError as exc:
        raise _INVALID_CREDENTIALS from exc
    return _token_pair(tokens)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request,
    session: SessionDep,
    current_user: CurrentUserDep,
    payload: RefreshRequest | None = None,
) -> Response:
    """Revoke the caller's refresh token, or all of them when none is given."""
    AuthService(session).logout(
        user_id=current_user.id,
        refresh_token=payload.refresh_token if payload else None,
        context=_context(request),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me")
def read_me(session: SessionDep, current_user: CurrentUserDep) -> MeResponse:
    """Return who the caller is, what roles they hold and where they apply."""
    user = UserRepository(session).get_by_id(current_user.id)
    if user is None:
        raise _INVALID_CREDENTIALS

    roles_repository = UserRoleRepository(session)
    granted = [
        GrantedRole(
            role=grant.role,
            label=ROLE_META[grant.role].label,
            short_label=ROLE_META[grant.role].short_label,
            expires_at=grant.expires_at,
        )
        for grant in roles_repository.list_global_roles(current_user.id)
    ]
    predio_grants = list(roles_repository.list_predio_roles(current_user.id))
    granted.extend(
        GrantedRole(
            role=grant.role,
            label=ROLE_META[grant.role].label,
            short_label=ROLE_META[grant.role].short_label,
            predio_id=grant.predio_id,
            expires_at=grant.expires_at,
        )
        for grant in predio_grants
    )

    enrolment = TwoFactorRepository(session).get_for_user(current_user.id)
    return MeResponse(
        user=UserRead.model_validate(user),
        roles=granted,
        active_role=current_user.active_role,
        predios_accesibles=sorted({grant.predio_id for grant in predio_grants}),
        two_factor_enabled=bool(enrolment and enrolment.enabled),
    )


@router.post("/switch-role")
def switch_role(
    payload: SwitchRoleRequest,
    request: Request,
    session: SessionDep,
    current_user: CurrentUserDep,
) -> AccessTokenResponse:
    """Re-issue an access token under another role the caller already holds."""
    try:
        token, expires_in = AuthService(session).switch_role(
            user_id=current_user.id,
            role=payload.role,
            from_role=current_user.active_role,
            two_factor_verified=current_user.two_factor_verified,
            context=_context(request),
        )
    except RoleNotGrantedError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Role not granted"
        ) from exc
    return AccessTokenResponse(
        access_token=token, expires_in=expires_in, active_role=payload.role
    )


__all__ = ["PURPOSE_ACCESS", "router"]
