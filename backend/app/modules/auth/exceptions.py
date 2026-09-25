"""Domain exceptions of the auth context.

Deliberately free of imports from this module's models or service, so any other
module can catch these without risking an import cycle.
"""

from __future__ import annotations

from app.shared.exceptions import DomainError


class AuthError(DomainError):
    """Base class for authentication failures."""


class InvalidCredentialsError(AuthError):
    """Wrong email or password. Deliberately indistinguishable to the caller."""


class AccountLockedError(AuthError):
    """Too many failed attempts; the account is temporarily locked."""


class EmailAlreadyRegisteredError(AuthError):
    """That email already has an account."""


class InvalidTwoFactorCodeError(AuthError):
    """The submitted second factor did not verify."""


class TwoFactorLockedError(AuthError):
    """Too many failed second-factor attempts."""


class TwoFactorNotEnrolledError(AuthError):
    """The account has no usable second factor."""


class TwoFactorRequiredForRoleError(AuthError):
    """One of the caller's roles makes 2FA mandatory, so it cannot be disabled."""


class InvalidRefreshTokenError(AuthError):
    """The refresh token is unknown, expired or already revoked."""


class RoleNotGrantedError(AuthError):
    """The caller does not hold the role they asked to act under."""
