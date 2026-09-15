"""Shared pytest fixtures."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

# Settings require secrets and connection strings. Provide deterministic test
# values before any application module builds the settings singleton.
_TEST_ENVIRONMENT = {
    "SECRET_KEY": "test-secret-key-with-at-least-32-characters",
    "TOTP_SECRET_ENCRYPTION_KEY": "test-totp-encryption-key-at-least-32-chars",
    "DATABASE_URL": "postgresql+psycopg://test:test@localhost:5432/test",
    "REDIS_URL": "redis://localhost:6379/15",
}
for _key, _value in _TEST_ENVIRONMENT.items():
    os.environ.setdefault(_key, _value)


@pytest.fixture
def client() -> Iterator[TestClient]:
    """Return a test client bound to a freshly built application."""
    from app.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client
