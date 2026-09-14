"""Application configuration loaded from the environment."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    """Deployment environment the application runs in."""

    DEVELOPMENT = "development"
    TESTING = "testing"
    PRODUCTION = "production"


class Settings(BaseSettings):
    """Runtime settings; every value can be overridden via environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    app_name: str = "AgroVista API"
    app_version: str = "0.2.0"
    environment: Environment = Environment.DEVELOPMENT
    debug: bool = False

    database_url: str = (
        "postgresql+psycopg://agrovista:agrovista@localhost:5432/agrovista"
    )
    redis_url: str = "redis://localhost:6379/0"

    jwt_secret: str = Field(
        default="change-me-in-production-with-a-32-byte-secret", min_length=32
    )
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 30

    log_level: str = "INFO"
    cors_origins: tuple[str, ...] = ("http://localhost:5173",)

    @property
    def is_production(self) -> bool:
        """Whether the app runs with production semantics."""
        return self.environment is Environment.PRODUCTION


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
