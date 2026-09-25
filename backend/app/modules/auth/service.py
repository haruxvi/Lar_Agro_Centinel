"""Authentication flows: registration, login, 2FA, refresh rotation, roles."""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import lru_cache
from typing import Final

from sqlalchemy.orm import Session

from app.modules.audit import events
from app.modules.audit.service import AuditService
from app.modules.auth import security as passwords
from app.modules.auth import two_factor
from app.modules.auth.exceptions import (
    AccountLockedError,
    EmailAlreadyRegisteredError,
    InvalidCredentialsError,
    InvalidRefreshTokenError,
    InvalidTwoFactorCodeError,
    RoleNotGrantedError,
    TwoFactorLockedError,
    TwoFactorNotEnrolledError,
    TwoFactorRequiredForRoleError,
)
from app.modules.auth.models import RefreshToken, UserTwoFactor
from app.modules.auth.repository import RefreshTokenRepository, TwoFactorRepository
from app.modules.users.models import User
from app.modules.users.repository import UserRepository, UserRoleRepository
from app.shared.config import get_settings
from app.shared.enums import AuditEventSeverity, AuditOutcome
from app.shared.roles import ROLES_2FA_REQUIRED, Role
from app.shared.security import (
    PURPOSE_TWO_FACTOR,
    InvalidTokenError,
    create_access_token,
    create_challenge_token,
    create_refresh_token,
    decode_token,
    hash_refresh_token,
)

MAX_LOGIN_ATTEMPTS: Final = 5
LOGIN_LOCKOUT_DURATION: Final = timedelta(minutes=15)
CHALLENGE_TOKEN_TTL: Final = timedelta(minutes=5)

_ROLE_ORDER: Final = {role: index for index, role in enumerate(Role)}


class LoginStatus(StrEnum):
    """What the password step concluded."""

    AUTHENTICATED = "AUTHENTICATED"
    TWO_FACTOR_REQUIRED = "TWO_FACTOR_REQUIRED"
    TWO_FACTOR_ENROLLMENT_REQUIRED = "TWO_FACTOR_ENROLLMENT_REQUIRED"


@dataclass(frozen=True)
class IssuedTokens:
    """A freshly issued token pair."""

    access_token: str
    refresh_token: str
    expires_in: int


@dataclass(frozen=True)
class LoginResult:
    """Outcome of the password step."""

    status: LoginStatus
    tokens: IssuedTokens | None = None
    challenge_token: str | None = None


@dataclass(frozen=True)
class RequestContext:
    """Where a request came from, for the audit trail."""

    ip: str | None = None
    user_agent: str | None = None


@lru_cache(maxsize=1)
def _dummy_password_hash() -> str:
    """Return a throwaway hash, so a failed login costs what a real one costs."""
    return passwords.hash_password(secrets.token_urlsafe(32))


class AuthService:
    """Entry point of the auth context's public API."""

    def __init__(self, session: Session) -> None:
        """Bind the service to a database session."""
        self._session = session
        self._users = UserRepository(session)
        self._roles = UserRoleRepository(session)
        self._two_factor = TwoFactorRepository(session)
        self._refresh = RefreshTokenRepository(session)
        self._audit = AuditService(session)

    # --- internals ------------------------------------------------------------

    def _authority(self, user_id: uuid.UUID) -> tuple[list[Role], list[uuid.UUID]]:
        """Return the user's live roles and the predios they can reach."""
        global_grants = self._roles.list_global_roles(user_id)
        predio_grants = list(self._roles.list_predio_roles(user_id))
        roles = {grant.role for grant in global_grants} | {
            grant.role for grant in predio_grants
        }
        return (
            sorted(roles, key=lambda role: _ROLE_ORDER[role]),
            sorted({grant.predio_id for grant in predio_grants}),
        )

    def _issue_tokens(
        self,
        user: User,
        context: RequestContext,
        *,
        two_factor_verified: bool,
        active_role: Role | None = None,
    ) -> IssuedTokens:
        settings = get_settings()
        roles, predio_ids = self._authority(user.id)
        resolved_role = active_role or (roles[0] if roles else None)

        access_token = create_access_token(
            user.id,
            roles,
            resolved_role,
            predio_ids,
            two_factor_verified=two_factor_verified,
        )
        plain, hashed = create_refresh_token(user.id)
        self._refresh.add(
            RefreshToken(
                user_id=user.id,
                token_hash=hashed,
                expires_at=datetime.now(UTC)
                + timedelta(days=settings.jwt_refresh_token_expire_days),
                user_agent=context.user_agent,
                ip_address=context.ip,
            )
        )
        user.last_login_at = datetime.now(UTC)
        return IssuedTokens(
            access_token=access_token,
            refresh_token=plain,
            expires_in=settings.jwt_access_token_expire_minutes * 60,
        )

    def _is_locked(self, user: User) -> bool:
        return user.locked_until is not None and user.locked_until > datetime.now(UTC)

    def _register_login_failure(self, user: User, context: RequestContext) -> None:
        user.failed_login_attempts = (user.failed_login_attempts or 0) + 1
        if user.failed_login_attempts >= MAX_LOGIN_ATTEMPTS:
            user.locked_until = datetime.now(UTC) + LOGIN_LOCKOUT_DURATION
            user.failed_login_attempts = 0
            self._audit.record_security(
                event_type=events.ACCOUNT_LOCKED,
                action="lock account after repeated failures",
                outcome=AuditOutcome.DENIED,
                severity=AuditEventSeverity.WARNING,
                actor_user_id=user.id,
                actor_ip=context.ip,
                actor_user_agent=context.user_agent,
                details={
                    "locked_minutes": int(LOGIN_LOCKOUT_DURATION.total_seconds() // 60)
                },
            )

    def _requires_two_factor(self, roles: list[Role]) -> bool:
        return bool(ROLES_2FA_REQUIRED.intersection(roles))

    # --- registration ---------------------------------------------------------

    def register(
        self,
        *,
        email: str,
        password: str,
        full_name: str,
        rut: str | None,
        phone: str | None,
        context: RequestContext,
    ) -> User:
        """Create an account with no roles. A Propietario grants them later."""
        normalized_email = email.strip().lower()
        if self._users.get_by_email(normalized_email) is not None:
            raise EmailAlreadyRegisteredError

        user = User(
            email=normalized_email,
            password_hash=passwords.hash_password(password),
            full_name=full_name.strip(),
            rut=rut,
            phone=phone,
        )
        self._users.add(user)
        self._audit.record_security(
            event_type=events.USER_REGISTERED,
            action="register account",
            outcome=AuditOutcome.SUCCESS,
            actor_user_id=user.id,
            actor_ip=context.ip,
            actor_user_agent=context.user_agent,
            target_resource_type="user",
            target_resource_id=user.id,
        )
        self._session.commit()
        return user

    # --- login ----------------------------------------------------------------

    def authenticate(
        self, *, email: str, password: str, context: RequestContext
    ) -> LoginResult:
        """Verify credentials and decide whether a second factor is needed."""
        user = self._users.get_by_email(email)

        if user is None:
            # Verify against a dummy hash anyway: a missing account must cost
            # the same as a wrong password, or the response time enumerates users.
            passwords.verify_password(password, _dummy_password_hash())
            self._audit.record_security(
                event_type=events.LOGIN_FAILED,
                action="authenticate",
                outcome=AuditOutcome.FAILURE,
                severity=AuditEventSeverity.WARNING,
                actor_ip=context.ip,
                actor_user_agent=context.user_agent,
                details={"reason": "unknown_account"},
            )
            self._session.commit()
            raise InvalidCredentialsError

        if self._is_locked(user):
            self._audit.record_security(
                event_type=events.LOGIN_FAILED,
                action="authenticate",
                outcome=AuditOutcome.DENIED,
                severity=AuditEventSeverity.WARNING,
                actor_user_id=user.id,
                actor_ip=context.ip,
                actor_user_agent=context.user_agent,
                details={"reason": "account_locked"},
            )
            self._session.commit()
            raise AccountLockedError

        if (
            not passwords.verify_password(password, user.password_hash)
            or not user.is_active
        ):
            self._register_login_failure(user, context)
            self._audit.record_security(
                event_type=events.LOGIN_FAILED,
                action="authenticate",
                outcome=AuditOutcome.FAILURE,
                severity=AuditEventSeverity.WARNING,
                actor_user_id=user.id,
                actor_ip=context.ip,
                actor_user_agent=context.user_agent,
                details={"reason": "invalid_password" if user.is_active else "inactive"},
            )
            self._session.commit()
            raise InvalidCredentialsError

        user.failed_login_attempts = 0
        if passwords.needs_rehash(user.password_hash):
            # Raising the cost over time must not force password resets.
            user.password_hash = passwords.hash_password(password)
            self._audit.record_security(
                event_type=events.PASSWORD_REHASHED,
                action="rehash password with current parameters",
                outcome=AuditOutcome.SUCCESS,
                actor_user_id=user.id,
                actor_ip=context.ip,
            )

        roles, _ = self._authority(user.id)
        enrolment = self._two_factor.get_for_user(user.id)

        if enrolment is not None and enrolment.enabled:
            challenge = create_challenge_token(user.id, CHALLENGE_TOKEN_TTL)
            self._audit.record_security(
                event_type=events.LOGIN_2FA_REQUIRED,
                action="authenticate",
                outcome=AuditOutcome.SUCCESS,
                actor_user_id=user.id,
                actor_ip=context.ip,
                actor_user_agent=context.user_agent,
            )
            self._session.commit()
            return LoginResult(LoginStatus.TWO_FACTOR_REQUIRED, challenge_token=challenge)

        if self._requires_two_factor(roles):
            challenge = create_challenge_token(user.id, CHALLENGE_TOKEN_TTL)
            self._audit.record_security(
                event_type=events.LOGIN_2FA_REQUIRED,
                action="authenticate",
                outcome=AuditOutcome.SUCCESS,
                actor_user_id=user.id,
                actor_ip=context.ip,
                actor_user_agent=context.user_agent,
                details={"reason": "enrollment_required"},
            )
            self._session.commit()
            return LoginResult(
                LoginStatus.TWO_FACTOR_ENROLLMENT_REQUIRED, challenge_token=challenge
            )

        tokens = self._issue_tokens(user, context, two_factor_verified=False)
        self._audit.record_security(
            event_type=events.LOGIN_SUCCESS,
            action="authenticate",
            outcome=AuditOutcome.SUCCESS,
            actor_user_id=user.id,
            actor_role=roles[0] if roles else None,
            actor_ip=context.ip,
            actor_user_agent=context.user_agent,
        )
        self._session.commit()
        return LoginResult(LoginStatus.AUTHENTICATED, tokens=tokens)

    # --- second factor --------------------------------------------------------

    def subject_from_challenge(self, challenge_token: str) -> uuid.UUID:
        """Return the user a challenge token belongs to, or reject it."""
        try:
            payload = decode_token(challenge_token)
        except InvalidTokenError as exc:
            raise InvalidCredentialsError from exc
        if payload.purpose != PURPOSE_TWO_FACTOR:
            raise InvalidCredentialsError
        return payload.sub

    def verify_two_factor(
        self, *, challenge_token: str, code: str, context: RequestContext
    ) -> IssuedTokens:
        """Complete a login by verifying a TOTP or recovery code."""
        user_id = self.subject_from_challenge(challenge_token)
        user = self._users.get_by_id(user_id)
        if user is None or not user.is_active:
            raise InvalidCredentialsError

        enrolment = self._two_factor.get_for_user(user_id)
        if enrolment is None or not enrolment.enabled:
            raise TwoFactorNotEnrolledError

        if two_factor.is_locked(enrolment):
            self._audit.record_security(
                event_type=events.TWO_FACTOR_LOCKED,
                action="verify second factor",
                outcome=AuditOutcome.DENIED,
                severity=AuditEventSeverity.WARNING,
                actor_user_id=user_id,
                actor_ip=context.ip,
            )
            self._session.commit()
            raise TwoFactorLockedError

        outcome = two_factor.verify_totp(enrolment, code)

        if outcome is not two_factor.TwoFactorOutcome.SUCCESS:
            used_recovery = self._consume_recovery_code(enrolment, code, context)
            if not used_recovery:
                locked = two_factor.is_locked(enrolment)
                self._audit.record_security(
                    event_type=events.TWO_FACTOR_LOCKED
                    if locked
                    else events.TWO_FACTOR_FAILED,
                    action="verify second factor",
                    outcome=AuditOutcome.FAILURE,
                    severity=AuditEventSeverity.WARNING,
                    actor_user_id=user_id,
                    actor_ip=context.ip,
                    actor_user_agent=context.user_agent,
                    details={"outcome": outcome.value},
                )
                self._session.commit()
                raise TwoFactorLockedError if locked else InvalidTwoFactorCodeError

        tokens = self._issue_tokens(user, context, two_factor_verified=True)
        self._audit.record_security(
            event_type=events.TWO_FACTOR_VERIFIED,
            action="verify second factor",
            outcome=AuditOutcome.SUCCESS,
            actor_user_id=user_id,
            actor_ip=context.ip,
            actor_user_agent=context.user_agent,
        )
        self._audit.record_security(
            event_type=events.LOGIN_SUCCESS,
            action="authenticate",
            outcome=AuditOutcome.SUCCESS,
            actor_user_id=user_id,
            actor_ip=context.ip,
            actor_user_agent=context.user_agent,
        )
        self._session.commit()
        return tokens

    def _consume_recovery_code(
        self, enrolment: UserTwoFactor, code: str, context: RequestContext
    ) -> bool:
        """Spend a recovery code if it matches. Returns whether one was used."""
        valid, used_hash = two_factor.verify_recovery_code(
            code, list(enrolment.recovery_codes_hash or [])
        )
        if not valid or used_hash is None:
            return False

        # Reassign rather than mutate: SQLAlchemy tracks arrays by identity.
        enrolment.recovery_codes_hash = [
            digest for digest in enrolment.recovery_codes_hash if digest != used_hash
        ]
        two_factor.register_success(enrolment)
        self._audit.record_security(
            event_type=events.TWO_FACTOR_RECOVERY_CODE_USED,
            action="verify second factor with recovery code",
            outcome=AuditOutcome.SUCCESS,
            severity=AuditEventSeverity.WARNING,
            actor_user_id=enrolment.user_id,
            actor_ip=context.ip,
            details={"remaining_codes": len(enrolment.recovery_codes_hash)},
        )
        return True

    def start_enrollment(
        self, *, user_id: uuid.UUID, context: RequestContext
    ) -> tuple[str, str, str]:
        """Begin 2FA enrolment. Returns ``(secret, uri, qr_code)``."""
        user = self._users.get_by_id(user_id)
        if user is None:
            raise InvalidCredentialsError

        secret = two_factor.generate_secret()
        enrolment = self._two_factor.get_for_user(user_id) or UserTwoFactor(
            user_id=user_id
        )
        enrolment.totp_secret_encrypted = two_factor.encrypt_secret(secret)
        enrolment.totp_verified = False
        enrolment.enabled = False
        enrolment.failed_attempts = 0
        enrolment.locked_until = None
        self._two_factor.save(enrolment)

        uri = two_factor.generate_provisioning_uri(secret, user.email)
        self._audit.record_security(
            event_type=events.TWO_FACTOR_ENROLLMENT_STARTED,
            action="start 2fa enrolment",
            outcome=AuditOutcome.SUCCESS,
            actor_user_id=user_id,
            actor_ip=context.ip,
        )
        self._session.commit()
        return secret, uri, two_factor.generate_qr_code(uri)

    def confirm_enrollment(
        self, *, user_id: uuid.UUID, code: str, context: RequestContext
    ) -> list[str]:
        """Confirm enrolment with a live code and return the recovery codes."""
        enrolment = self._two_factor.get_for_user(user_id)
        if enrolment is None:
            raise TwoFactorNotEnrolledError

        secret = two_factor.decrypt_secret(enrolment.totp_secret_encrypted)
        if not two_factor.verify_code(secret, code):
            two_factor.register_failure(enrolment)
            self._audit.record_security(
                event_type=events.TWO_FACTOR_FAILED,
                action="confirm 2fa enrolment",
                outcome=AuditOutcome.FAILURE,
                severity=AuditEventSeverity.WARNING,
                actor_user_id=user_id,
                actor_ip=context.ip,
            )
            self._session.commit()
            raise InvalidTwoFactorCodeError

        plain_codes, hashes = two_factor.generate_recovery_codes()
        enrolment.enabled = True
        enrolment.totp_verified = True
        enrolment.enrolled_at = datetime.now(UTC)
        enrolment.recovery_codes_hash = hashes
        two_factor.register_success(enrolment)

        self._audit.record_security(
            event_type=events.TWO_FACTOR_ENABLED,
            action="enable 2fa",
            outcome=AuditOutcome.SUCCESS,
            actor_user_id=user_id,
            actor_ip=context.ip,
            details={"recovery_codes_issued": len(plain_codes)},
        )
        self._session.commit()
        return plain_codes

    def disable_two_factor(
        self, *, user_id: uuid.UUID, code: str, context: RequestContext
    ) -> None:
        """Disable 2FA, unless one of the user's roles requires it."""
        roles, _ = self._authority(user_id)
        if self._requires_two_factor(roles):
            self._audit.record_security(
                event_type=events.TWO_FACTOR_DISABLED,
                action="disable 2fa",
                outcome=AuditOutcome.DENIED,
                severity=AuditEventSeverity.WARNING,
                actor_user_id=user_id,
                actor_ip=context.ip,
                details={"reason": "role_requires_2fa"},
            )
            self._session.commit()
            raise TwoFactorRequiredForRoleError

        enrolment = self._two_factor.get_for_user(user_id)
        if enrolment is None or not enrolment.enabled:
            raise TwoFactorNotEnrolledError

        if (
            two_factor.verify_totp(enrolment, code)
            is not two_factor.TwoFactorOutcome.SUCCESS
        ):
            self._session.commit()
            raise InvalidTwoFactorCodeError

        self._two_factor.delete_for_user(user_id)
        self._audit.record_security(
            event_type=events.TWO_FACTOR_DISABLED,
            action="disable 2fa",
            outcome=AuditOutcome.SUCCESS,
            severity=AuditEventSeverity.WARNING,
            actor_user_id=user_id,
            actor_ip=context.ip,
        )
        self._session.commit()

    # --- sessions -------------------------------------------------------------

    def refresh(self, *, refresh_token: str, context: RequestContext) -> IssuedTokens:
        """Rotate a refresh token, detecting reuse of an already-spent one."""
        stored = self._refresh.get_by_hash(hash_refresh_token(refresh_token))
        if stored is None:
            raise InvalidRefreshTokenError

        if stored.revoked_at is not None:
            # A revoked token coming back means it was captured: the whole
            # family is burned, not just this one.
            revoked = self._refresh.revoke_all_for_user(stored.user_id, "reuse_detected")
            self._audit.record_security(
                event_type=events.REFRESH_TOKEN_REUSE_DETECTED,
                action="refresh session",
                outcome=AuditOutcome.DENIED,
                severity=AuditEventSeverity.CRITICAL,
                actor_user_id=stored.user_id,
                actor_ip=context.ip,
                actor_user_agent=context.user_agent,
                details={"revoked_sessions": revoked},
            )
            self._session.commit()
            raise InvalidRefreshTokenError

        if stored.expires_at <= datetime.now(UTC):
            raise InvalidRefreshTokenError

        user = self._users.get_by_id(stored.user_id)
        if user is None or not user.is_active or self._is_locked(user):
            raise InvalidRefreshTokenError

        self._refresh.revoke(stored, "rotated")
        tokens = self._issue_tokens(user, context, two_factor_verified=False)
        self._audit.record_security(
            event_type=events.TOKEN_REFRESHED,
            action="refresh session",
            outcome=AuditOutcome.SUCCESS,
            actor_user_id=user.id,
            actor_ip=context.ip,
            actor_user_agent=context.user_agent,
        )
        self._session.commit()
        return tokens

    def logout(
        self, *, user_id: uuid.UUID, refresh_token: str | None, context: RequestContext
    ) -> None:
        """Revoke the session's refresh token, or all of them when none is given."""
        if refresh_token:
            stored = self._refresh.get_by_hash(hash_refresh_token(refresh_token))
            if (
                stored is not None
                and stored.user_id == user_id
                and stored.revoked_at is None
            ):
                self._refresh.revoke(stored, "logout")
        else:
            self._refresh.revoke_all_for_user(user_id, "logout")

        self._audit.record_security(
            event_type=events.LOGOUT,
            action="logout",
            outcome=AuditOutcome.SUCCESS,
            actor_user_id=user_id,
            actor_ip=context.ip,
            actor_user_agent=context.user_agent,
        )
        self._session.commit()

    def switch_role(
        self,
        *,
        user_id: uuid.UUID,
        role: Role,
        from_role: Role | None,
        two_factor_verified: bool,
        context: RequestContext,
    ) -> tuple[str, int]:
        """Re-issue an access token under another role the user already holds."""
        roles, predio_ids = self._authority(user_id)
        if role not in roles:
            self._audit.record_security(
                event_type=events.AUTHORIZATION_DENIED,
                action="switch role",
                outcome=AuditOutcome.DENIED,
                severity=AuditEventSeverity.WARNING,
                actor_user_id=user_id,
                actor_role=from_role,
                actor_ip=context.ip,
                details={"requested_role": role.value},
            )
            self._session.commit()
            raise RoleNotGrantedError

        settings = get_settings()
        token = create_access_token(
            user_id,
            roles,
            role,
            predio_ids,
            two_factor_verified=two_factor_verified,
        )
        self._audit.record_security(
            event_type=events.ROLE_SWITCHED,
            action="switch role",
            outcome=AuditOutcome.SUCCESS,
            actor_user_id=user_id,
            actor_role=role,
            actor_ip=context.ip,
            details={
                "from_role": from_role.value if from_role else None,
                "to_role": role.value,
            },
        )
        self._session.commit()
        return token, settings.jwt_access_token_expire_minutes * 60
