"""Argon2id password hashing."""

from __future__ import annotations

import pytest
from argon2 import PasswordHasher

from app.modules.auth.security import (
    hash_password,
    needs_rehash,
    verify_password,
)
from app.shared.config import get_settings

PASSWORD = "Cordillera-Sur-2026"  # noqa: S105 - test fixture, not a credential


def test_hash_is_argon2id() -> None:
    assert hash_password(PASSWORD).startswith("$argon2id$")


def test_same_password_hashes_differently() -> None:
    # A random salt per hash: identical passwords must not collide in storage.
    assert hash_password(PASSWORD) != hash_password(PASSWORD)


def test_hash_embeds_the_configured_parameters() -> None:
    settings = get_settings()
    hashed = hash_password(PASSWORD)
    assert f"m={settings.argon2_memory_cost}" in hashed
    assert f"t={settings.argon2_time_cost}" in hashed
    assert f"p={settings.argon2_parallelism}" in hashed


def test_correct_password_verifies() -> None:
    assert verify_password(PASSWORD, hash_password(PASSWORD)) is True


def test_wrong_password_does_not_verify() -> None:
    assert verify_password("Cordillera-Norte-2026", hash_password(PASSWORD)) is False


@pytest.mark.parametrize(
    "corrupt",
    [
        "",
        "not-a-hash",
        "$argon2id$v=19$m=65536,t=3,p=2$truncated",
        "$2b$12$abcdefghijklmnopqrstuv",  # a bcrypt hash from another system
    ],
)
def test_corrupt_hash_returns_false_without_raising(corrupt: str) -> None:
    assert verify_password(PASSWORD, corrupt) is False


def test_current_parameters_do_not_need_rehash() -> None:
    assert needs_rehash(hash_password(PASSWORD)) is False


def test_weaker_parameters_need_rehash() -> None:
    settings = get_settings()
    weaker = PasswordHasher(
        time_cost=max(1, settings.argon2_time_cost - 1),
        memory_cost=settings.argon2_memory_cost // 2,
        parallelism=settings.argon2_parallelism,
        hash_len=32,
        salt_len=16,
    )
    legacy_hash = weaker.hash(PASSWORD)

    assert needs_rehash(legacy_hash) is True
    # The old hash still verifies, which is what makes a transparent rehash on
    # the next successful login possible.
    assert verify_password(PASSWORD, legacy_hash) is True


def test_unusable_hash_is_reported_as_needing_rehash() -> None:
    assert needs_rehash("not-a-hash") is True
