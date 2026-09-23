"""Public DTOs of the users context."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, EmailStr

from app.shared.roles import Role

_RUT_PATTERN = re.compile(r"^(\d{7,8})-([0-9K])$")


def _check_digit(body: str) -> str:
    """Return the Chilean RUT check digit for ``body`` (modulo 11)."""
    total = 0
    factor = 2
    for digit in reversed(body):
        total += int(digit) * factor
        factor = 2 if factor == 7 else factor + 1
    remainder = 11 - (total % 11)
    if remainder == 11:
        return "0"
    if remainder == 10:
        return "K"
    return str(remainder)


def normalize_rut(value: str) -> str:
    """Normalize a Chilean RUT to ``12345678-5`` form and verify its check digit.

    Accepts the usual written forms (``12.345.678-5``, ``123456785``) and
    raises ``ValueError`` when the check digit does not match.
    """
    cleaned = value.strip().upper().replace(".", "").replace(" ", "")
    if "-" not in cleaned and len(cleaned) >= 2:
        cleaned = f"{cleaned[:-1]}-{cleaned[-1]}"

    match = _RUT_PATTERN.match(cleaned)
    if match is None:
        raise ValueError("RUT must look like 12345678-5")

    body, check = match.groups()
    if _check_digit(body) != check:
        raise ValueError("RUT check digit is invalid")
    return f"{body}-{check}"


Rut = Annotated[str, AfterValidator(normalize_rut)]


class UserRead(BaseModel):
    """User representation exposed to API clients."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    full_name: str
    rut: str | None = None
    phone: str | None = None
    is_active: bool
    email_verified: bool
    created_at: datetime
    last_login_at: datetime | None = None


class UserRoleRead(BaseModel):
    """A role granted to a user, with its presentation metadata."""

    model_config = ConfigDict(from_attributes=True)

    role: Role
    label: str
    short_label: str
    predio_id: uuid.UUID | None = None
    expires_at: datetime | None = None
