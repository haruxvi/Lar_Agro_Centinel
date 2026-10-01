"""Application configuration loaded from the environment."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, RedisDsn, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Placeholder secrets that ship in documentation and templates. A deployment
# that still carries one of these is misconfigured, and the app must not start.
_EXAMPLE_SECRET_PREFIXES = ("change", "dev_secret", "your_", "example", "replace")
_EXAMPLE_SECRETS = frozenset(
    {
        "secret",
        "password",
        "another_32_chars_min_random_key_yyyyy",
        "insecure",
    }
)


def _looks_like_an_example_secret(value: str) -> bool:
    """Return whether a secret is one of the well-known placeholders."""
    normalized = value.strip().lower()
    return normalized in _EXAMPLE_SECRETS or normalized.startswith(
        _EXAMPLE_SECRET_PREFIXES
    )


# Public OAuth2 client-credentials endpoint of Copernicus Data Space.
_CDSE_OAUTH_ENDPOINT = (
    "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/"
    "protocol/openid-connect/token"
)


def _secret_value(secret: SecretStr | None) -> str:
    """Return a secret's stripped value, or "" when it is absent."""
    return secret.get_secret_value().strip() if secret is not None else ""


class Settings(BaseSettings):
    """Runtime settings; every value can be overridden via environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # App metadata
    app_name: str = "Lar Agro Centinel"
    app_version: str = "0.1.0"
    environment: Literal["development", "staging", "production"] = "development"
    debug: bool = False

    # Security - JWT
    secret_key: SecretStr = Field(..., min_length=32)
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 30
    jwt_refresh_token_expire_days: int = 7
    jwt_issuer: str = "lar-agro-centinel"
    jwt_leeway_seconds: int = 30

    # Security - passwords (Argon2id, see docs/dependencies.md)
    # Calibrated to ~280 ms per hash; see docs/dependencies.md. Each concurrent
    # hash holds argon2_memory_cost of RAM, so re-measure on the target host.
    argon2_time_cost: int = 5
    argon2_memory_cost: int = 131072  # KiB, i.e. 128 MiB
    argon2_parallelism: int = 2
    password_min_length: int = 12

    # 2FA
    totp_issuer: str = "Lar Agro Centinel"
    totp_valid_window: int = 1
    totp_secret_encryption_key: SecretStr = Field(..., min_length=32)
    recovery_codes_count: int = 10

    # Database
    # Runtime connection: the least-privileged application role.
    database_url: PostgresDsn
    # Migration connection: the schema owner. Used by Alembic only.
    database_migration_url: PostgresDsn
    database_pool_size: int = 10
    database_max_overflow: int = 20
    database_echo: bool = False

    # Redis
    redis_url: RedisDsn
    redis_cache_ttl_seconds: int = 300

    # Rate limiting
    rate_limit_default: str = "100/minute"
    rate_limit_auth: str = "5/15minutes"
    rate_limit_2fa: str = "5/15minutes"
    # Switched off only where throttling would test the limiter instead of
    # the endpoint (the suite enables it explicitly where it is the subject).
    rate_limit_enabled: bool = True

    # CORS
    cors_allowed_origins: list[str] = ["http://localhost:5173"]
    cors_allow_credentials: bool = True

    # Logging
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "json"

    # --- Sentinel Hub / Copernicus Data Space ---
    # Optional: without credentials the app starts and the analysis module
    # reports itself unavailable (see sentinel_configured).
    sentinel_client_id: SecretStr | None = None
    sentinel_client_secret: SecretStr | None = None
    sentinel_base_url: str = "https://sh.dataspace.copernicus.eu"
    sentinel_token_url: str = _CDSE_OAUTH_ENDPOINT
    sentinel_request_timeout_s: int = Field(120, gt=0)
    sentinel_max_retries: int = Field(3, ge=0)
    sentinel_monthly_pu_budget: int = Field(30_000, gt=0)
    # Cloud cover is measured over the predio at low resolution: only a
    # percentage is needed, not an image (see ADR-004).
    sentinel_cloud_stats_resolution_m: int = Field(60, gt=0)
    sentinel_cloud_stats_cache_ttl_s: int = Field(21_600, gt=0)  # 6 h
    sentinel_circuit_failure_threshold: int = Field(5, gt=0)
    sentinel_circuit_reset_s: int = Field(300, gt=0)

    # --- Analysis ---
    analysis_max_cloud_coverage: float = Field(0.30, gt=0, le=1)  # over the predio
    analysis_min_valid_pixels_ratio: float = Field(0.60, gt=0, le=1)
    analysis_default_date_range_days: int = Field(30, gt=0)
    analysis_max_date_range_days: int = Field(365, gt=0)
    analysis_resolution_m: int = Field(10, gt=0)  # native for B04/B08
    analysis_max_predio_area_m2: float = Field(100_000_000.0, gt=0)  # 10.000 ha
    analysis_job_timeout_s: int = Field(900, gt=0)
    # A PENDING analysis whose job never started (lost from the queue, or the
    # enqueue itself failed). It holds the one-in-flight slot of its predio,
    # so it must expire. Generous: two jobs run at a time, so a busy queue
    # legitimately keeps analyses waiting for a while.
    analysis_pending_timeout_s: int = Field(3600, gt=0)
    # SCL class 6 (water). Excluded by default: on agricultural predios a pond
    # or a flooded corner would drag every statistic towards negative NDVI.
    analysis_exclude_water: bool = True

    # --- Per-lote statistics ---
    # A lote needs BOTH: enough pixels to be stable, and enough of itself
    # visible to be representative.
    analysis_lote_min_valid_pixels: int = Field(20, gt=0)  # 0.2 ha at 10 m
    analysis_lote_min_valid_ratio: float = Field(0.50, gt=0, le=1)
    # Erodes each lote before measuring, to drop mixed border pixels. Off by
    # default until real data shows how it interacts with the thresholds.
    analysis_lote_inner_buffer_m: float = Field(0.0, ge=0)

    # --- Anomaly detection ---
    anomaly_zscore_threshold: float = Field(-2.0, lt=0)
    anomaly_min_cluster_pixels: int = Field(10, gt=0)
    anomaly_simplify_tolerance_m: float = Field(2.0, ge=0)
    # Provisional severity cuts, not validated agronomically: to be calibrated
    # against reviewed anomalies (see ADR-004).
    anomaly_severity_high_zscore: float = Field(-3.0, lt=0)
    anomaly_severity_high_area_m2: float = Field(5_000.0, gt=0)
    anomaly_severity_high_area_ratio: float = Field(0.10, gt=0, le=1)

    # --- Raster storage ---
    # "local" is not fit for production on ephemeral filesystems (KL-005).
    storage_backend: Literal["local", "s3"] = "local"
    storage_local_path: str = "./data/rasters"
    storage_s3_bucket: str | None = None
    storage_s3_region: str | None = None

    # Geospatial (see docs/decisions/ADR-003-geospatial-model.md)
    geo_srid: int = 4326
    geo_max_polygon_vertices: int = 10_000
    geo_max_geojson_size_kb: int = 2048
    # Sanity bounds against capture errors (swapped coordinates produce absurd
    # polygons), not business rules.
    geo_min_predio_area_m2: float = 100.0  # 0.01 ha
    geo_max_predio_area_m2: float = 500_000_000.0  # 50.000 ha
    geo_lote_containment_tolerance_m: float = 5.0
    # An automatic repair is accepted without confirmation when its area change
    # is under EITHER bound: the ratio protects large geometries, the absolute
    # floor forgives digitising noise on small ones. See assess_repair().
    geo_repair_max_area_change_ratio: float = 0.01  # 1%
    geo_repair_ignore_below_m2: float = 50.0

    # Storage
    storage_path: str = "./data"
    max_upload_size_mb: int = 50

    # Audit retention (Ley 21.719 and SAG regulations)
    audit_retention_days_security: int = 365
    audit_retention_days_domain: int = 1825  # 5 years
    audit_retention_days_products: int = 3650  # 10 years (SAG)

    @model_validator(mode="after")
    def _validate_security(self) -> Settings:
        """Refuse to start with an insecure configuration.

        Fail-closed: a missing or placeholder security setting stops the
        process instead of silently degrading into an insecure default.
        """
        secret = self.secret_key.get_secret_value()
        totp_key = self.totp_secret_encryption_key.get_secret_value()

        if _looks_like_an_example_secret(secret):
            raise ValueError("SECRET_KEY is a placeholder value; generate a real one")
        if _looks_like_an_example_secret(totp_key):
            raise ValueError(
                "TOTP_SECRET_ENCRYPTION_KEY is a placeholder value; generate a real one"
            )
        if secret == totp_key:
            raise ValueError(
                "SECRET_KEY and TOTP_SECRET_ENCRYPTION_KEY must be different: "
                "one signs sessions, the other encrypts stored 2FA secrets"
            )

        if self.environment == "production":
            if self.debug:
                raise ValueError("DEBUG must be false in production")
            if "*" in self.cors_allowed_origins:
                raise ValueError(
                    "CORS_ALLOWED_ORIGINS must not contain '*' in production"
                )

        return self

    @model_validator(mode="after")
    def _validate_analysis(self) -> Settings:
        """Refuse to start with an analysis configuration that cannot work.

        Missing Sentinel credentials are allowed (the module reports itself
        unavailable); a placeholder or contradictory value is not.
        """
        for name in ("sentinel_client_id", "sentinel_client_secret"):
            value = _secret_value(getattr(self, name))
            if value and _looks_like_an_example_secret(value):
                raise ValueError(
                    f"{name.upper()} is a placeholder value; set the real one or "
                    "leave it empty to run without satellite analysis"
                )

        if self.storage_backend == "s3" and not (
            (self.storage_s3_bucket or "").strip()
            and (self.storage_s3_region or "").strip()
        ):
            raise ValueError(
                "STORAGE_BACKEND=s3 requires STORAGE_S3_BUCKET and STORAGE_S3_REGION"
            )

        if self.analysis_default_date_range_days > self.analysis_max_date_range_days:
            raise ValueError(
                "ANALYSIS_DEFAULT_DATE_RANGE_DAYS cannot exceed "
                "ANALYSIS_MAX_DATE_RANGE_DAYS"
            )
        if self.analysis_pending_timeout_s < self.analysis_job_timeout_s:
            # Shorter than one run, it would expire analyses merely waiting
            # behind a job in progress.
            raise ValueError(
                "ANALYSIS_PENDING_TIMEOUT_S cannot be shorter than ANALYSIS_JOB_TIMEOUT_S"
            )
        if self.anomaly_severity_high_zscore > self.anomaly_zscore_threshold:
            # A HIGH cut above the detection threshold would make every
            # detected anomaly qualify on z-score alone.
            raise ValueError(
                "ANOMALY_SEVERITY_HIGH_ZSCORE must be at or below "
                "ANOMALY_ZSCORE_THRESHOLD"
            )
        return self

    @property
    def sentinel_configured(self) -> bool:
        """Return whether both Sentinel Hub credentials are present and non-empty.

        ``.env.example`` ships them empty, which pydantic reads as an empty
        string rather than None: empty counts as missing.
        """
        return bool(
            _secret_value(self.sentinel_client_id)
            and _secret_value(self.sentinel_client_secret)
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
