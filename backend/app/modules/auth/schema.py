"""Public DTOs of the auth context."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, EmailStr, Field

from app.modules.auth.security import validate_password_policy
from app.modules.users.schema import Rut, UserRead
from app.shared.roles import Role

Password = Annotated[str, AfterValidator(validate_password_policy)]
"""A password that satisfies the policy. Never stored or logged in this form."""


class RegisterRequest(BaseModel):
    """Self-registration payload. Roles are granted afterwards by a Propietario."""

    email: EmailStr
    password: Password
    full_name: str = Field(min_length=1, max_length=255)
    rut: Rut | None = None
    phone: str | None = Field(default=None, max_length=32)


class RegisterResponse(BaseModel):
    """Identifier of the account just created. No session is issued."""

    user_id: uuid.UUID


class LoginRequest(BaseModel):
    """Credentials. The password is not policy-checked here: it is verified."""

    email: EmailStr
    password: str


class LoginResponse(BaseModel):
    """Outcome of the password step.

    Either a session was issued, or a second factor is needed and the caller
    receives a short-lived challenge instead.
    """

    requires_2fa: bool = False
    requires_2fa_enrollment: bool = False
    challenge_token: str | None = None
    access_token: str | None = None
    refresh_token: str | None = None
    token_type: str = "bearer"  # noqa: S105 - OAuth2 response field, not a secret
    expires_in: int | None = None


class TokenPair(BaseModel):
    """Tokens issued after a successful authentication."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"  # noqa: S105 - OAuth2 response field, not a secret
    expires_in: int


class AccessTokenResponse(BaseModel):
    """A re-issued access token, for instance after switching role."""

    access_token: str
    token_type: str = "bearer"  # noqa: S105 - OAuth2 response field, not a secret
    expires_in: int
    active_role: Role | None = None


class TwoFactorVerifyRequest(BaseModel):
    """A challenge token plus a TOTP or recovery code."""

    challenge_token: str
    code: str = Field(min_length=1, max_length=32)


class TwoFactorEnrollResponse(BaseModel):
    """Everything an authenticator app needs to enrol."""

    secret: str
    uri: str
    qr_code: str


class TwoFactorCodeRequest(BaseModel):
    """A TOTP code, used to confirm or disable enrolment."""

    code: str = Field(min_length=1, max_length=32)


class TwoFactorConfirmResponse(BaseModel):
    """Recovery codes, shown once and never again."""

    recovery_codes: list[str]
    warning: str = (
        "Store these recovery codes somewhere safe. They are shown once and "
        "cannot be retrieved later. Each code works a single time."
    )


class RefreshRequest(BaseModel):
    """A refresh token being exchanged for a new pair."""

    refresh_token: str


class SwitchRoleRequest(BaseModel):
    """The role the caller wants to act under."""

    role: Role


class GrantedRole(BaseModel):
    """A role the caller holds, with its presentation metadata."""

    model_config = ConfigDict(from_attributes=True)

    role: Role
    label: str
    short_label: str
    predio_id: uuid.UUID | None = None
    expires_at: datetime | None = None


class MeResponse(BaseModel):
    """Who the caller is and what they may act as."""

    user: UserRead
    roles: list[GrantedRole]
    active_role: Role | None = None
    predios_accesibles: list[uuid.UUID]
    two_factor_enabled: bool


class StatusResponse(BaseModel):
    """A plain acknowledgement for operations that return no data."""

    status: str
