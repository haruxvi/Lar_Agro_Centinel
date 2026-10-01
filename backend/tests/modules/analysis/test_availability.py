"""Satellite analysis switches itself off without credentials; the app does not."""

from __future__ import annotations

from typing import Any

import pytest

from app.main import create_app
from app.modules.analysis.availability import (
    analysis_available,
    ensure_analysis_available,
)
from app.modules.analysis.exceptions import AnalysisUnavailableError
from app.shared.config import Settings

BASE: dict[str, Any] = {
    "secret_key": "a-real-secret-key-with-at-least-32-chars",
    "totp_secret_encryption_key": "a-different-real-key-of-32-chars-min",
    "database_url": "postgresql+psycopg://lar_app:pw@localhost:5432/db",
    "database_migration_url": "postgresql+psycopg://lar_owner:pw@localhost:5432/db",
    "redis_url": "redis://localhost:6379/0",
    "rate_limit_enabled": False,
}


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **{**BASE, **overrides})


def test_without_credentials_analysis_is_unavailable() -> None:
    settings = _settings()
    assert analysis_available(settings) is False
    with pytest.raises(AnalysisUnavailableError):
        ensure_analysis_available(settings)


def test_with_credentials_analysis_is_available() -> None:
    settings = _settings(
        sentinel_client_id="sh-1234", sentinel_client_secret="real-sentinel-secret"
    )
    assert analysis_available(settings) is True
    ensure_analysis_available(settings)  # does not raise


def test_the_unavailable_error_explains_itself_without_secrets() -> None:
    payload = AnalysisUnavailableError().as_dict()
    assert payload["error"] == "AnalysisUnavailableError"
    assert "credenciales de Sentinel Hub" in str(payload["message"])


# create_app() configures logging with basicConfig(force=True), which replaces
# every root handler, caplog's included, and writes to stdout. Startup logs are
# therefore read from captured stdout, where they really go.


def test_the_app_starts_without_credentials_and_says_why_analysis_is_off(
    capsys: pytest.CaptureFixture[str],
) -> None:
    app = create_app(_settings())
    assert app is not None
    assert "analysis_unavailable" in capsys.readouterr().out


def test_the_app_starts_quietly_with_credentials(
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = _settings(
        sentinel_client_id="sh-1234", sentinel_client_secret="real-sentinel-secret"
    )
    create_app(settings)
    output = capsys.readouterr().out
    assert "analysis_unavailable" not in output
    assert "real-sentinel-secret" not in output
