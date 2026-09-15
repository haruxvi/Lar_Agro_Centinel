"""Application configuration loaded from the environment."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, RedisDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # Security - passwords
    password_hash_rounds: int = 12

    # 2FA
    totp_issuer: str = "Lar Agro Centinel"
    totp_valid_window: int = 1
    totp_secret_encryption_key: SecretStr = Field(..., min_length=32)
    recovery_codes_count: int = 10

    # Database
    database_url: PostgresDsn
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

    # CORS
    cors_allowed_origins: list[str] = ["http://localhost:5173"]
    cors_allow_credentials: bool = True

    # Logging
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "json"

    # External services
    sentinel_client_id: SecretStr | None = None
    sentinel_client_secret: SecretStr | None = None

    # Storage
    storage_path: str = "./data"
    max_upload_size_mb: int = 50

    # Audit retention (Ley 21.719 and SAG regulations)
    audit_retention_days_security: int = 365
    audit_retention_days_domain: int = 1825  # 5 years
    audit_retention_days_products: int = 3650  # 10 years (SAG)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
