"""TOTP enrolment, verification, recovery codes, replay guard and lockout."""

from __future__ import annotations

import base64
import re
import uuid
from datetime import UTC, datetime, timedelta

import pyotp
import pytest
import redis

from app.modules.auth.models import UserTwoFactor
from app.modules.auth.two_factor import (
    MAX_FAILED_ATTEMPTS,
    SECRET_LENGTH,
    TwoFactorOutcome,
    decrypt_secret,
    encrypt_secret,
    generate_provisioning_uri,
    generate_qr_code,
    generate_recovery_codes,
    generate_secret,
    hash_recovery_code,
    is_locked,
    register_failure,
    register_success,
    verify_code,
    verify_recovery_code,
    verify_totp,
)
from app.shared.config import get_settings

BASE32_ALPHABET = re.compile(r"^[A-Z2-7]+$")
RECOVERY_CODE_FORMAT = re.compile(r"^[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}$")
TOTP_STEP_SECONDS = 30


def _enrolment(secret: str | None = None) -> UserTwoFactor:
    return UserTwoFactor(
        user_id=uuid.uuid4(),
        totp_secret_encrypted=encrypt_secret(secret or generate_secret()),
        totp_verified=True,
        enabled=True,
        recovery_codes_hash=[],
        failed_attempts=0,
        locked_until=None,
    )


# --- secret handling ----------------------------------------------------------


def test_secret_is_base32_of_the_configured_length() -> None:
    secret = generate_secret()
    assert len(secret) == SECRET_LENGTH
    assert BASE32_ALPHABET.match(secret)


def test_secrets_are_unique() -> None:
    assert len({generate_secret() for _ in range(20)}) == 20


def test_encrypt_decrypt_roundtrip() -> None:
    secret = generate_secret()
    assert decrypt_secret(encrypt_secret(secret)) == secret


def test_ciphertext_never_contains_the_plaintext_secret() -> None:
    secret = generate_secret()
    encrypted = encrypt_secret(secret)
    assert secret not in encrypted
    assert secret.lower() not in encrypted.lower()


def test_encrypting_twice_produces_different_ciphertext() -> None:
    # Fernet uses a random IV, so storage does not reveal equal secrets.
    secret = generate_secret()
    assert encrypt_secret(secret) != encrypt_secret(secret)


# --- enrolment ----------------------------------------------------------------


def test_provisioning_uri_identifies_issuer_and_account() -> None:
    secret = generate_secret()
    uri = generate_provisioning_uri(secret, "agronoma@example.cl")

    assert uri.startswith("otpauth://totp/")
    assert secret in uri
    assert "agronoma%40example.cl" in uri
    assert get_settings().totp_issuer.replace(" ", "%20") in uri


def test_qr_code_is_a_png_data_uri() -> None:
    uri = generate_provisioning_uri(generate_secret(), "agronoma@example.cl")
    data_uri = generate_qr_code(uri)

    assert data_uri.startswith("data:image/png;base64,")
    payload = base64.b64decode(data_uri.removeprefix("data:image/png;base64,"))
    assert payload.startswith(b"\x89PNG\r\n\x1a\n")


# --- code verification --------------------------------------------------------


def test_current_code_verifies() -> None:
    secret = generate_secret()
    assert verify_code(secret, pyotp.TOTP(secret).now()) is True


def test_wrong_code_is_rejected() -> None:
    secret = generate_secret()
    current = pyotp.TOTP(secret).now()
    # Derive a code that is guaranteed to differ from the valid one.
    wrong = f"{(int(current) + 1) % 1_000_000:06d}"
    assert verify_code(secret, wrong) is False


@pytest.mark.parametrize("malformed", ["", "abc", "12345", "1234567", "  "])
def test_malformed_code_is_rejected(malformed: str) -> None:
    assert verify_code(generate_secret(), malformed) is False


def test_previous_window_still_verifies() -> None:
    # Clock skew tolerance: the immediately previous step is accepted.
    secret = generate_secret()
    previous = pyotp.TOTP(secret).at(
        datetime.now(UTC) - timedelta(seconds=TOTP_STEP_SECONDS)
    )
    assert verify_code(secret, previous) is True


def test_two_windows_back_is_rejected() -> None:
    secret = generate_secret()
    older = pyotp.TOTP(secret).at(
        datetime.now(UTC) - timedelta(seconds=TOTP_STEP_SECONDS * 3)
    )
    assert verify_code(secret, older) is False


# --- recovery codes -----------------------------------------------------------


def test_recovery_codes_match_the_configured_count_and_format() -> None:
    plain, hashes = generate_recovery_codes()
    assert len(plain) == get_settings().recovery_codes_count
    assert len(hashes) == len(plain)
    assert all(RECOVERY_CODE_FORMAT.match(code) for code in plain)


def test_recovery_codes_are_unique_and_stored_hashed() -> None:
    plain, hashes = generate_recovery_codes()
    assert len(set(plain)) == len(plain)
    assert all(code not in hashes for code in plain)
    assert all(len(digest) == 64 for digest in hashes)


def test_valid_recovery_code_is_accepted_and_identifies_its_hash() -> None:
    plain, hashes = generate_recovery_codes()
    valid, used = verify_recovery_code(plain[3], hashes)

    assert valid is True
    assert used == hash_recovery_code(plain[3])


def test_recovery_code_comparison_ignores_case_and_separators() -> None:
    plain, hashes = generate_recovery_codes()
    messy = f"  {plain[0].lower().replace('-', ' ')}  "
    assert verify_recovery_code(messy, hashes)[0] is True


def test_consumed_recovery_code_is_rejected_afterwards() -> None:
    plain, hashes = generate_recovery_codes()
    _, used = verify_recovery_code(plain[0], hashes)
    assert used is not None

    remaining = [digest for digest in hashes if digest != used]
    valid, _ = verify_recovery_code(plain[0], remaining)
    assert valid is False


def test_unknown_recovery_code_is_rejected() -> None:
    _, hashes = generate_recovery_codes()
    assert verify_recovery_code("ZZZZ-ZZZZ-ZZZZ", hashes) == (False, None)


# --- lockout ------------------------------------------------------------------


def test_five_failures_trigger_a_lockout() -> None:
    enrolment = _enrolment()
    locked = [register_failure(enrolment) for _ in range(MAX_FAILED_ATTEMPTS)]

    assert locked[:-1] == [False] * (MAX_FAILED_ATTEMPTS - 1)
    assert locked[-1] is True
    assert is_locked(enrolment) is True


def test_lockout_expires() -> None:
    enrolment = _enrolment()
    for _ in range(MAX_FAILED_ATTEMPTS):
        register_failure(enrolment)

    assert enrolment.locked_until is not None
    later = enrolment.locked_until + timedelta(seconds=1)
    assert is_locked(enrolment, now=later) is False


def test_a_locked_enrolment_rejects_even_a_valid_code() -> None:
    secret = generate_secret()
    enrolment = _enrolment(secret)
    for _ in range(MAX_FAILED_ATTEMPTS):
        register_failure(enrolment)

    outcome = verify_totp(enrolment, pyotp.TOTP(secret).now())
    assert outcome is TwoFactorOutcome.LOCKED


def test_success_clears_the_failure_state() -> None:
    enrolment = _enrolment()
    register_failure(enrolment)
    register_failure(enrolment)

    register_success(enrolment)
    assert enrolment.failed_attempts == 0
    assert enrolment.locked_until is None
    assert enrolment.last_used_at is not None


# --- replay guard (needs Redis) -----------------------------------------------


def test_valid_code_is_accepted_once(redis_client: redis.Redis) -> None:
    secret = generate_secret()
    enrolment = _enrolment(secret)
    assert verify_totp(enrolment, pyotp.TOTP(secret).now()) is TwoFactorOutcome.SUCCESS


def test_the_same_code_cannot_be_used_twice(redis_client: redis.Redis) -> None:
    secret = generate_secret()
    enrolment = _enrolment(secret)
    code = pyotp.TOTP(secret).now()

    assert verify_totp(enrolment, code) is TwoFactorOutcome.SUCCESS
    assert verify_totp(enrolment, code) is TwoFactorOutcome.REPLAYED


def test_a_replay_counts_as_a_failed_attempt(redis_client: redis.Redis) -> None:
    secret = generate_secret()
    enrolment = _enrolment(secret)
    code = pyotp.TOTP(secret).now()

    verify_totp(enrolment, code)
    verify_totp(enrolment, code)
    assert enrolment.failed_attempts == 1


def test_replay_guard_is_scoped_per_user(redis_client: redis.Redis) -> None:
    secret = generate_secret()
    code = pyotp.TOTP(secret).now()
    first, second = _enrolment(secret), _enrolment(secret)

    assert verify_totp(first, code) is TwoFactorOutcome.SUCCESS
    # A different user submitting the same code is not a replay.
    assert verify_totp(second, code) is TwoFactorOutcome.SUCCESS


def test_invalid_code_reports_invalid_not_replayed(redis_client: redis.Redis) -> None:
    enrolment = _enrolment()
    assert verify_totp(enrolment, "000000") is TwoFactorOutcome.INVALID_CODE
    assert enrolment.failed_attempts == 1
