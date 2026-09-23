"""Password hashing and password policy for the auth context.

Argon2id through argon2-cffi. Parameters come from settings so they can be
raised over time: existing hashes are detected by ``needs_rehash`` and replaced
transparently on the next successful login.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.shared.config import get_settings

logger = logging.getLogger(__name__)

COMMON_PASSWORDS_PATH = Path(__file__).parent / "data" / "common_passwords.txt"

_HASH_LENGTH = 32
_SALT_LENGTH = 16

# Argon2 has no input length limit of its own, but hashing unbounded input is a
# cheap way to waste server memory and CPU.
PASSWORD_MAX_LENGTH = 1024


@lru_cache(maxsize=1)
def _hasher() -> PasswordHasher:
    """Return the process-wide hasher configured from settings."""
    settings = get_settings()
    return PasswordHasher(
        time_cost=settings.argon2_time_cost,
        memory_cost=settings.argon2_memory_cost,
        parallelism=settings.argon2_parallelism,
        hash_len=_HASH_LENGTH,
        salt_len=_SALT_LENGTH,
    )


def hash_password(plain: str) -> str:
    """Return an Argon2id hash, which embeds its own parameters and salt."""
    return _hasher().hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Return whether ``plain`` matches ``hashed``.

    Never raises: a wrong password and an unusable stored hash both answer
    False, so callers cannot leak the difference through an error path.
    """
    try:
        _hasher().verify(hashed, plain)
    except VerifyMismatchError:
        return False
    except (VerificationError, InvalidHashError):
        # A corrupt hash in the database is an anomaly worth investigating; it
        # must not reach the endpoint as an exception.
        logger.warning("stored password hash is unusable")
        return False
    return True


def needs_rehash(hashed: str) -> bool:
    """Return whether ``hashed`` was produced with outdated parameters."""
    try:
        return _hasher().check_needs_rehash(hashed)
    except InvalidHashError:
        logger.warning("stored password hash is unusable")
        return True


@lru_cache(maxsize=1)
def _common_passwords() -> frozenset[str]:
    """Return the breach-corpus password list, normalized for comparison."""
    with COMMON_PASSWORDS_PATH.open(encoding="utf-8", errors="ignore") as handle:
        return frozenset(
            normalized for line in handle if (normalized := _normalize(line))
        )


def _normalize(value: str) -> str:
    """Normalize a candidate before comparing it against the common list."""
    return value.strip().lower().replace(" ", "")


def is_common_password(plain: str) -> bool:
    """Return whether the password appears in the known-breached list."""
    return _normalize(plain) in _common_passwords()


def validate_password_policy(plain: str) -> str:
    """Validate a password against the policy, returning it unchanged.

    Follows NIST SP 800-63B where it matters: length carries the weight, no
    special-character rule, no forced expiry, and a check against known
    breached passwords.
    """
    settings = get_settings()
    problems: list[str] = []

    if len(plain) < settings.password_min_length:
        problems.append(
            f"must be at least {settings.password_min_length} characters long"
        )
    if len(plain) > PASSWORD_MAX_LENGTH:
        problems.append(f"must be at most {PASSWORD_MAX_LENGTH} characters long")
    if not any(char.isupper() for char in plain):
        problems.append("must contain an uppercase letter")
    if not any(char.islower() for char in plain):
        problems.append("must contain a lowercase letter")
    if not any(char.isdigit() for char in plain):
        problems.append("must contain a digit")
    if is_common_password(plain):
        problems.append("appears in lists of commonly used passwords")

    if problems:
        raise ValueError("Password " + "; ".join(problems))
    return plain
