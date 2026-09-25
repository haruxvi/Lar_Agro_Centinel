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

    # External services
    sentinel_client_id: SecretStr | None = None
    sentinel_client_secret: SecretStr | None = None

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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
