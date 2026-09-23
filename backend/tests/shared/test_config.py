"""Settings defaults and fail-closed validation."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from app.shared.config import Settings

REAL_SECRET = "a-real-secret-key-with-at-least-32-chars"  # noqa: S105 - test fixture
REAL_TOTP_KEY = "a-different-real-key-of-32-chars-min"  # noqa: S105 - test fixture
DATABASE_URL = "postgresql+psycopg://lar_app:pw@localhost:5432/db"
MIGRATION_URL = "postgresql+psycopg://lar_owner:pw@localhost:5432/db"


def _settings(**overrides: Any) -> Settings:
    """Build settings from explicit values only, ignoring the environment."""
    values: dict[str, Any] = {
        "secret_key": REAL_SECRET,
        "totp_secret_encryption_key": REAL_TOTP_KEY,
        "database_url": DATABASE_URL,
        "database_migration_url": MIGRATION_URL,
        "redis_url": "redis://localhost:6379/0",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_defaults_describe_the_application() -> None:
    settings = _settings()
    assert settings.app_name == "Lar Agro Centinel"
    assert settings.app_version == "0.1.0"
    assert settings.jwt_issuer == "lar-agro-centinel"
    assert settings.audit_retention_days_products == 3650


def test_secrets_are_not_exposed_in_repr() -> None:
    settings = _settings()
    assert REAL_SECRET not in repr(settings)
    assert REAL_TOTP_KEY not in repr(settings)


# --- fail-closed: none of these configurations may start the app -------------


def test_missing_secret_key_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SECRET_KEY", raising=False)
    values = {
        "totp_secret_encryption_key": REAL_TOTP_KEY,
        "database_url": DATABASE_URL,
        "database_migration_url": MIGRATION_URL,
        "redis_url": "redis://localhost:6379/0",
    }
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "short",
    ["", "short", "x" * 31],
)
def test_short_secret_key_is_rejected(short: str) -> None:
    with pytest.raises(ValidationError):
        _settings(secret_key=short)


@pytest.mark.parametrize(
    "placeholder",
    [
        "change_this_to_32_chars_min_random_string_xxxxxx",
        "change_me_change_me_change_me_change_me",
        "CHANGEME_CHANGEME_CHANGEME_CHANGEME_1234",
        "dev_secret_change_in_prod_dev_secret_1234",
        "your_secret_key_goes_here_0123456789abcd",
        "example_secret_example_secret_example_01",
    ],
)
def test_placeholder_secret_key_is_rejected(placeholder: str) -> None:
    with pytest.raises(ValidationError, match="placeholder"):
        _settings(secret_key=placeholder)


def test_placeholder_totp_key_is_rejected() -> None:
    with pytest.raises(ValidationError, match="placeholder"):
        _settings(totp_secret_encryption_key="another_32_chars_min_random_key_yyyyy")


def test_identical_keys_are_rejected() -> None:
    # One signs sessions, the other encrypts stored 2FA secrets. Reusing a
    # single key means a leak of either compromises both.
    with pytest.raises(ValidationError, match="must be different"):
        _settings(secret_key=REAL_SECRET, totp_secret_encryption_key=REAL_SECRET)


def test_debug_in_production_is_rejected() -> None:
    with pytest.raises(ValidationError, match="DEBUG"):
        _settings(environment="production", debug=True)


def test_wildcard_cors_in_production_is_rejected() -> None:
    with pytest.raises(ValidationError, match="CORS"):
        _settings(environment="production", cors_allowed_origins=["*"])


def test_production_with_a_sound_configuration_starts() -> None:
    settings = _settings(
        environment="production",
        debug=False,
        cors_allowed_origins=["https://app.laragrocentinel.cl"],
    )
    assert settings.environment == "production"


def test_wildcard_cors_is_allowed_outside_production() -> None:
    # Convenience in development must not be blocked; only production is strict.
    settings = _settings(environment="development", cors_allowed_origins=["*"])
    assert settings.cors_allowed_origins == ["*"]
