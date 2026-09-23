"""Password policy: length, composition and known-breached passwords."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from app.modules.auth.schema import Password
from app.modules.auth.security import (
    PASSWORD_MAX_LENGTH,
    is_common_password,
    validate_password_policy,
)


class _Form(BaseModel):
    password: Password


@pytest.mark.parametrize(
    "candidate",
    [
        "Cordillera-Sur-2026",
        "vinaTinajas2026",
        "TresPalomas9alazul",
    ],
)
def test_strong_passwords_are_accepted(candidate: str) -> None:
    assert validate_password_policy(candidate) == candidate
    assert _Form(password=candidate).password == candidate


@pytest.mark.parametrize(
    ("candidate", "reason"),
    [
        ("Corto1", "at least 12"),
        ("cordillerasur2026", "uppercase"),
        ("CORDILLERASUR2026", "lowercase"),
        ("CordilleraSurSinNumero", "digit"),
        ("A" * (PASSWORD_MAX_LENGTH + 1) + "a1", "at most"),
    ],
)
def test_weak_passwords_are_rejected(candidate: str, reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        validate_password_policy(candidate)


def test_common_password_is_rejected_even_when_it_satisfies_composition() -> None:
    # 12 characters, upper, lower and a digit: passes every composition rule.
    candidate = "Password1234"  # noqa: S105 - test fixture, not a credential
    assert len(candidate) >= 12
    assert any(c.isupper() for c in candidate)
    assert any(c.islower() for c in candidate)
    assert any(c.isdigit() for c in candidate)

    with pytest.raises(ValueError, match="commonly used"):
        validate_password_policy(candidate)


@pytest.mark.parametrize(
    "candidate",
    [
        "password1234",
        "PASSWORD1234",
        "  Password1234  ",
        "Pass word1234",
    ],
)
def test_common_password_check_normalizes_case_and_spaces(candidate: str) -> None:
    assert is_common_password(candidate) is True


def test_uncommon_password_is_not_flagged() -> None:
    assert is_common_password("TresPalomas9alazul") is False


def test_pydantic_rejects_a_weak_password() -> None:
    with pytest.raises(ValidationError):
        _Form(password="corto")


def test_error_lists_every_problem_at_once() -> None:
    with pytest.raises(ValueError) as exc_info:
        validate_password_policy("corto")
    message = str(exc_info.value)
    assert "at least 12" in message
    assert "uppercase" in message
    assert "digit" in message
