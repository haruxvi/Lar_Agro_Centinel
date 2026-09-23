"""TOTP-based two-factor authentication and recovery codes.

The TOTP secret is encrypted with Fernet before it reaches the database, every
code is single-use, and repeated failures lock the enrolment.
"""

from __future__ import annotations

import base64
import hashlib
import io
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import lru_cache

import pyotp
import qrcode
from cryptography.fernet import Fernet, InvalidToken
from qrcode.image.pil import PilImage

from app.modules.auth.models import UserTwoFactor
from app.shared.cache import claim_once
from app.shared.config import get_settings

SECRET_LENGTH = 32
RECOVERY_CODE_GROUPS = 3
RECOVERY_CODE_GROUP_SIZE = 4
# Excludes characters that are easy to confuse when read aloud or copied.
RECOVERY_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_DURATION = timedelta(minutes=15)

# A TOTP step is 30 s; 90 s covers the current window plus the skew tolerance.
REPLAY_GUARD_TTL_SECONDS = 90


class TwoFactorOutcome(StrEnum):
    """Result of verifying a second factor."""

    SUCCESS = "SUCCESS"
    INVALID_CODE = "INVALID_CODE"
    REPLAYED = "REPLAYED"
    LOCKED = "LOCKED"


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    """Return the Fernet box built from the configured encryption key.

    Fernet needs 32 url-safe base64 bytes; the configured key is an arbitrary
    string, so it is hashed to that shape.
    """
    key = get_settings().totp_secret_encryption_key.get_secret_value()
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def generate_secret() -> str:
    """Return a new base32 TOTP secret."""
    return pyotp.random_base32(length=SECRET_LENGTH)


def encrypt_secret(secret: str) -> str:
    """Encrypt a TOTP secret for storage."""
    return _fernet().encrypt(secret.encode("utf-8")).decode("utf-8")


def decrypt_secret(encrypted: str) -> str:
    """Decrypt a stored TOTP secret.

    Raises ``InvalidToken`` when the ciphertext was not produced by this key,
    which is the signal that the encryption key changed or the row was tampered
    with.
    """
    return _fernet().decrypt(encrypted.encode("utf-8")).decode("utf-8")


def generate_provisioning_uri(secret: str, user_email: str) -> str:
    """Return the otpauth:// URI an authenticator app scans."""
    settings = get_settings()
    return pyotp.TOTP(secret).provisioning_uri(
        name=user_email, issuer_name=settings.totp_issuer
    )


def generate_qr_code(uri: str) -> str:
    """Render a provisioning URI as a PNG data URI."""
    # Explicit factory: relying on the default means the image type depends on
    # whether Pillow happens to be installed.
    image = qrcode.make(uri, image_factory=PilImage)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def verify_code(secret: str, code: str) -> bool:
    """Return whether ``code`` is valid for ``secret`` right now."""
    settings = get_settings()
    return bool(
        pyotp.TOTP(secret).verify(code.strip(), valid_window=settings.totp_valid_window)
    )


def _normalize_recovery_code(code: str) -> str:
    return code.strip().upper().replace("-", "").replace(" ", "")


def hash_recovery_code(code: str) -> str:
    """Return the SHA-256 digest stored for a recovery code."""
    return hashlib.sha256(_normalize_recovery_code(code).encode("utf-8")).hexdigest()


def generate_recovery_codes() -> tuple[list[str], list[str]]:
    """Return ``(plaintext codes, hashes)``.

    The plaintext is shown to the user once and never stored.
    """
    settings = get_settings()
    plain: list[str] = []
    for _ in range(settings.recovery_codes_count):
        groups = [
            "".join(
                secrets.choice(RECOVERY_CODE_ALPHABET)
                for _ in range(RECOVERY_CODE_GROUP_SIZE)
            )
            for _ in range(RECOVERY_CODE_GROUPS)
        ]
        plain.append("-".join(groups))
    return plain, [hash_recovery_code(code) for code in plain]


def verify_recovery_code(code: str, stored_hashes: list[str]) -> tuple[bool, str | None]:
    """Return ``(valid, used_hash)``. The caller removes the used hash."""
    candidate = hash_recovery_code(code)
    for stored in stored_hashes:
        # Constant-time comparison: recovery codes are credentials.
        if secrets.compare_digest(candidate, stored):
            return True, stored
    return False, None


def is_locked(enrolment: UserTwoFactor, *, now: datetime | None = None) -> bool:
    """Return whether the enrolment is currently locked out."""
    if enrolment.locked_until is None:
        return False
    return (now or datetime.now(UTC)) < enrolment.locked_until


def register_failure(enrolment: UserTwoFactor, *, now: datetime | None = None) -> bool:
    """Record a failed attempt. Returns whether this attempt caused a lockout."""
    moment = now or datetime.now(UTC)
    enrolment.failed_attempts = (enrolment.failed_attempts or 0) + 1
    if enrolment.failed_attempts >= MAX_FAILED_ATTEMPTS:
        enrolment.locked_until = moment + LOCKOUT_DURATION
        enrolment.failed_attempts = 0
        return True
    return False


def register_success(enrolment: UserTwoFactor, *, now: datetime | None = None) -> None:
    """Record a successful verification and clear the failure state."""
    enrolment.failed_attempts = 0
    enrolment.locked_until = None
    enrolment.last_used_at = now or datetime.now(UTC)


def claim_code(user_id: uuid.UUID, code: str) -> bool:
    """Claim a TOTP code for one-time use. False when it was already used."""
    return claim_once(
        f"2fa:used:{user_id}:{_normalize_recovery_code(code)}",
        REPLAY_GUARD_TTL_SECONDS,
    )


def verify_totp(
    enrolment: UserTwoFactor,
    code: str,
    *,
    now: datetime | None = None,
) -> TwoFactorOutcome:
    """Verify a TOTP code against an enrolment, applying lockout and replay rules.

    Order matters: a locked enrolment is rejected before the code is even
    checked, so a lockout cannot be bypassed by holding a valid code.
    """
    if is_locked(enrolment, now=now):
        return TwoFactorOutcome.LOCKED

    try:
        secret = decrypt_secret(enrolment.totp_secret_encrypted)
    except InvalidToken:
        register_failure(enrolment, now=now)
        return TwoFactorOutcome.INVALID_CODE

    if not verify_code(secret, code):
        register_failure(enrolment, now=now)
        return TwoFactorOutcome.INVALID_CODE

    if not claim_code(enrolment.user_id, code):
        # Valid code, already spent: someone is replaying an intercepted one.
        register_failure(enrolment, now=now)
        return TwoFactorOutcome.REPLAYED

    register_success(enrolment, now=now)
    return TwoFactorOutcome.SUCCESS
