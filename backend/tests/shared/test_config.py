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


# --- Phase 3: satellite analysis -------------------------------------------------


def test_analysis_defaults_match_the_phase_3_decisions() -> None:
    settings = _settings()
    assert settings.sentinel_base_url == "https://sh.dataspace.copernicus.eu"
    assert settings.sentinel_token_url.startswith("https://identity.dataspace.")
    assert settings.sentinel_monthly_pu_budget == 30_000
    assert settings.sentinel_cloud_stats_resolution_m == 60
    assert settings.sentinel_cloud_stats_cache_ttl_s == 21_600
    assert settings.analysis_max_cloud_coverage == 0.30
    assert settings.analysis_resolution_m == 10
    assert settings.analysis_lote_min_valid_pixels == 20
    assert settings.analysis_lote_min_valid_ratio == 0.50
    assert settings.analysis_lote_inner_buffer_m == 0.0
    assert settings.anomaly_zscore_threshold == -2.0
    assert settings.anomaly_severity_high_zscore == -3.0
    assert settings.anomaly_severity_high_area_m2 == 5_000.0
    assert settings.anomaly_severity_high_area_ratio == 0.10
    assert settings.storage_backend == "local"


@pytest.mark.parametrize(
    ("client_id", "client_secret", "configured"),
    [
        (None, None, False),
        ("", "", False),  # how .env.example ships them
        ("   ", "   ", False),
        ("sh-1234", None, False),  # half a credential is no credential
        (None, "s3cr3t-value", False),
        ("sh-1234", "s3cr3t-value", True),
    ],
)
def test_sentinel_is_configured_only_with_both_credentials(
    client_id: str | None, client_secret: str | None, configured: bool
) -> None:
    settings = _settings(
        sentinel_client_id=client_id, sentinel_client_secret=client_secret
    )
    assert settings.sentinel_configured is configured


def test_the_app_settings_start_without_sentinel_credentials() -> None:
    # The rest of the system must run without satellite access.
    assert _settings().sentinel_configured is False


def test_sentinel_secret_is_not_exposed_in_repr() -> None:
    settings = _settings(
        sentinel_client_id="sh-1234", sentinel_client_secret="real-sentinel-secret"
    )
    assert "real-sentinel-secret" not in repr(settings)


@pytest.mark.parametrize("field", ["sentinel_client_id", "sentinel_client_secret"])
def test_a_placeholder_sentinel_credential_is_rejected(field: str) -> None:
    with pytest.raises(ValidationError, match="placeholder"):
        _settings(**{field: "your_client_value_here"})


@pytest.mark.parametrize(
    "missing",
    [
        {"storage_s3_region": "sa-east-1"},
        {"storage_s3_bucket": "lar-rasters"},
        {"storage_s3_bucket": "  ", "storage_s3_region": "sa-east-1"},
        {},
    ],
)
def test_s3_storage_without_bucket_and_region_does_not_start(
    missing: dict[str, str],
) -> None:
    with pytest.raises(ValidationError, match="STORAGE_S3_BUCKET"):
        _settings(storage_backend="s3", **missing)


def test_s3_storage_with_bucket_and_region_is_accepted_by_settings() -> None:
    settings = _settings(
        storage_backend="s3",
        storage_s3_bucket="lar-rasters",
        storage_s3_region="sa-east-1",
    )
    assert settings.storage_backend == "s3"


@pytest.mark.parametrize(
    "overrides",
    [
        {"analysis_max_cloud_coverage": 1.5},
        {"analysis_max_cloud_coverage": 0},
        {"analysis_lote_min_valid_ratio": 1.2},
        {"analysis_lote_inner_buffer_m": -10.0},
        {"anomaly_zscore_threshold": 0.5},
        {"sentinel_monthly_pu_budget": 0},
        {"sentinel_max_retries": -1},
    ],
)
def test_out_of_range_analysis_values_do_not_start(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _settings(**overrides)


def test_the_default_date_range_cannot_exceed_the_maximum() -> None:
    with pytest.raises(ValidationError, match="DEFAULT_DATE_RANGE"):
        _settings(analysis_default_date_range_days=400, analysis_max_date_range_days=365)


def test_the_high_severity_cut_cannot_sit_above_the_detection_threshold() -> None:
    # Otherwise every detected anomaly would be HIGH on z-score alone.
    with pytest.raises(ValidationError, match="SEVERITY_HIGH_ZSCORE"):
        _settings(anomaly_zscore_threshold=-2.0, anomaly_severity_high_zscore=-1.5)


def test_pending_analyses_wait_at_least_one_run_before_expiring() -> None:
    # Shorter than a run, the sweeper would fail analyses merely queued behind
    # one in progress.
    with pytest.raises(ValidationError, match="ANALYSIS_PENDING_TIMEOUT_S"):
        _settings(analysis_job_timeout_s=900, analysis_pending_timeout_s=600)


def test_the_pending_timeout_defaults_above_the_job_timeout() -> None:
    settings = _settings()
    assert settings.analysis_pending_timeout_s >= settings.analysis_job_timeout_s
