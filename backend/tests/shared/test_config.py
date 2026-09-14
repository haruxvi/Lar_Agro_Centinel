"""Settings defaults and helpers."""

from __future__ import annotations

from app.shared.config import Environment, Settings


def test_defaults_target_local_development() -> None:
    settings = Settings()
    assert settings.environment is Environment.DEVELOPMENT
    assert settings.is_production is False
    assert settings.app_version == "0.2.0"


def test_production_environment_flags_production() -> None:
    settings = Settings(environment=Environment.PRODUCTION)
    assert settings.is_production is True
