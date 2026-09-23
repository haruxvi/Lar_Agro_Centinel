"""Chilean RUT normalization and check-digit validation."""

from __future__ import annotations

import pytest

from app.modules.users.schema import _check_digit, normalize_rut


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("12.345.678-5", "12345678-5"),
        ("12345678-5", "12345678-5"),
        ("123456785", "12345678-5"),
        (" 12.345.678-5 ", "12345678-5"),
        ("7.654.321-6", "7654321-6"),
        ("10.000.013-K", "10000013-K"),
        ("10000013k", "10000013-K"),
        ("10.000.004-0", "10000004-0"),
    ],
)
def test_valid_rut_is_normalized(written: str, expected: str) -> None:
    assert normalize_rut(written) == expected


@pytest.mark.parametrize(
    "written",
    [
        "12.345.678-9",  # wrong check digit
        "16982359-K",  # check digit is 6, not K
        "10000013-0",  # check digit is K, not 0
        "12345678",  # check digit missing: reads as body 1234567 + dv 8
        "1234-5",  # body too short
        "123456789-5",  # body too long
        "abcdefgh-1",  # not numeric
        "",
        "-5",
    ],
)
def test_invalid_rut_is_rejected(written: str) -> None:
    with pytest.raises(ValueError):
        normalize_rut(written)


def test_check_digit_covers_both_modulo_11_special_cases() -> None:
    # Remainder 10 renders as K, remainder 11 renders as 0.
    assert _check_digit("10000013") == "K"
    assert _check_digit("10000004") == "0"


def test_normalization_is_idempotent() -> None:
    once = normalize_rut("12.345.678-5")
    assert normalize_rut(once) == once
