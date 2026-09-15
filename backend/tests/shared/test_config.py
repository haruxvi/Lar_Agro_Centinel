"""Settings defaults and validation."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from app.shared.config import Settings


def _settings(**overrides: Any) -> Settings:
    """Build settings from the process environment only, ignoring .env files."""
    return Settings(_env_file=None, **overrides)


def test_defaults_describe_the_application() -> None:
    settings = _settings()
    assert settings.app_name == "Lar Agro Centinel"
    assert settings.app_version == "0.1.0"
    assert settings.jwt_issuer == "lar-agro-centinel"
    assert settings.audit_retention_days_products == 3650


def test_secret_key_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SECRET_KEY", raising=False)
    with pytest.raises(ValidationError):
        _settings()


def test_short_secret_key_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _settings(secret_key="too-short")


def test_secrets_are_not_exposed_in_repr() -> None:
    settings = _settings()
    assert settings.secret_key.get_secret_value() not in repr(settings)
