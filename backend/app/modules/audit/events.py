"""Audit event types recorded by the platform.

Constants rather than free-form strings: the event type is what queries and
retention rules key on, so a typo would quietly lose a record.
"""

from __future__ import annotations

from typing import Final

# Accounts
USER_REGISTERED: Final = "USER_REGISTERED"
PASSWORD_CHANGED: Final = "PASSWORD_CHANGED"  # noqa: S105 - event name, not a secret
PASSWORD_REHASHED: Final = "PASSWORD_REHASHED"  # noqa: S105 - event name, not a secret
ACCOUNT_LOCKED: Final = "ACCOUNT_LOCKED"

# Sessions
LOGIN_SUCCESS: Final = "LOGIN_SUCCESS"
LOGIN_FAILED: Final = "LOGIN_FAILED"
LOGIN_2FA_REQUIRED: Final = "LOGIN_2FA_REQUIRED"
LOGOUT: Final = "LOGOUT"
TOKEN_REFRESHED: Final = "TOKEN_REFRESHED"  # noqa: S105 - event name, not a secret
REFRESH_TOKEN_REUSE_DETECTED: Final = "REFRESH_TOKEN_REUSE_DETECTED"  # noqa: S105

# Two-factor authentication
TWO_FACTOR_ENROLLMENT_STARTED: Final = "2FA_ENROLLMENT_STARTED"
TWO_FACTOR_ENABLED: Final = "2FA_ENABLED"
TWO_FACTOR_DISABLED: Final = "2FA_DISABLED"
TWO_FACTOR_VERIFIED: Final = "2FA_VERIFIED"
TWO_FACTOR_FAILED: Final = "2FA_FAILED"
TWO_FACTOR_LOCKED: Final = "2FA_LOCKED"
TWO_FACTOR_RECOVERY_CODE_USED: Final = "2FA_RECOVERY_CODE_USED"

# Authorization
AUTHORIZATION_DENIED: Final = "AUTHORIZATION_DENIED"
ROLE_GRANTED: Final = "ROLE_GRANTED"
ROLE_REVOKED: Final = "ROLE_REVOKED"
ROLE_SWITCHED: Final = "ROLE_SWITCHED"
