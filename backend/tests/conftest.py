"""Shared pytest fixtures."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
import redis
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

REPO_ROOT = Path(__file__).resolve().parents[2]

# Settings require secrets and connection strings. Provide deterministic test
# values before any application module builds the settings singleton.
_TEST_ENVIRONMENT = {
    "SECRET_KEY": "test-secret-key-with-at-least-32-characters",
    "TOTP_SECRET_ENCRYPTION_KEY": "test-totp-encryption-key-at-least-32-chars",
    "DATABASE_URL": "postgresql+psycopg://test:test@localhost:5432/test",
    "DATABASE_MIGRATION_URL": "postgresql+psycopg://test:test@localhost:5432/test",
    "REDIS_URL": "redis://localhost:6379/15",
    "RATE_LIMIT_ENABLED": "false",
}
for _key, _value in _TEST_ENVIRONMENT.items():
    os.environ.setdefault(_key, _value)

# --- Sentinel Hub: no test may ever reach the real API ---------------------------
# Processing Units are a finite monthly quota: a suite that spends them leaves
# the project unable to work. Two layers:
#
# Layer 2 (the alarm) records which credentials the process was STARTED with,
# before layer 1 blanks them, so test_sentinel_guard can fail loudly in CI.
# Both the names the app reads (SENTINEL_*) and the conventional Sentinel Hub
# ones (SH_*), which is how a CI secret would most likely be injected.
SENTINEL_CREDENTIAL_VARIABLES = (
    "SENTINEL_CLIENT_ID",
    "SENTINEL_CLIENT_SECRET",
    "SH_CLIENT_ID",
    "SH_CLIENT_SECRET",
)
_SENTINEL_VARIABLES_IN_PROCESS_ENV = tuple(
    name for name in SENTINEL_CREDENTIAL_VARIABLES if os.environ.get(name, "").strip()
)
# Layer 1 (the protection): blank them before any settings are built.
# Environment variables outrank .env in pydantic-settings, so these empty
# values also override credentials in a developer's .env: with credentials
# there, the suite passes and never uses them.
for _name in SENTINEL_CREDENTIAL_VARIABLES:
    os.environ[_name] = ""


@pytest.fixture
def sentinel_variables_in_process_env() -> tuple[str, ...]:
    """Return the Sentinel credential variables the process was started with."""
    return _SENTINEL_VARIABLES_IN_PROCESS_ENV


# Keep the wait short: when PostgreSQL is down every integration test would
# otherwise pay the full TCP timeout before skipping.
_CONNECT_TIMEOUT_SECONDS = 3

# The suite connects with both database roles on purpose: the schema owner runs
# migrations, and the runtime role proves the audit_log privileges hold.
_DEFAULT_OWNER_URL = "postgresql+psycopg://lar_owner:lar_owner_password@localhost:5432/lar_agro_centinel_test"
_DEFAULT_APP_URL = (
    "postgresql+psycopg://lar_app:lar_app_password@localhost:5432/lar_agro_centinel_test"
)


def _owner_database_url() -> str:
    """Return the URL of the schema owner, used for migrations."""
    return os.environ.get("TEST_DATABASE_URL", _DEFAULT_OWNER_URL)


def _app_database_url() -> str:
    """Return the URL of the least-privileged runtime role."""
    return os.environ.get("TEST_APP_DATABASE_URL", _DEFAULT_APP_URL)


def _ensure_database_exists(url: str) -> None:
    """Create the test database when it does not exist yet."""
    target = make_url(url)
    admin_engine = create_engine(
        target.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        connect_args={"connect_timeout": _CONNECT_TIMEOUT_SECONDS},
    )
    try:
        with admin_engine.connect() as connection:
            exists = connection.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": target.database},
            ).scalar()
            if not exists:
                # Database names cannot be bound as parameters; the value comes
                # from our own configuration, never from user input.
                connection.execute(text(f'CREATE DATABASE "{target.database}"'))  # noqa: S608
    finally:
        admin_engine.dispose()


def _grant_runtime_role_access(url: str) -> None:
    """Let the runtime role reach the freshly created test database."""
    target = make_url(url)
    engine = create_engine(
        url,
        isolation_level="AUTOCOMMIT",
        connect_args={"connect_timeout": _CONNECT_TIMEOUT_SECONDS},
    )
    try:
        with engine.connect() as connection:
            connection.execute(
                text(f'GRANT CONNECT ON DATABASE "{target.database}" TO lar_app')  # noqa: S608
            )
            connection.execute(text("GRANT USAGE ON SCHEMA public TO lar_app"))
    finally:
        engine.dispose()


def _migrate(url: str) -> None:
    """Bring the test database up to head with Alembic, as the schema owner."""
    from alembic.config import Config

    from alembic import command

    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "backend" / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")


@pytest.fixture(scope="session")
def db_engine() -> Iterator[Engine]:
    """Return an engine bound to a migrated test database, as the schema owner.

    Skips locally when PostgreSQL is not running, but fails in CI: these tests
    are part of the deliverable and must not be silently skipped there.
    """
    url = _owner_database_url()
    try:
        _ensure_database_exists(url)
        engine = create_engine(
            url,
            pool_pre_ping=True,
            connect_args={"connect_timeout": _CONNECT_TIMEOUT_SECONDS},
        )
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except OperationalError as exc:
        if os.environ.get("CI"):
            raise
        pytest.skip(
            f"PostgreSQL is not reachable for integration tests: {exc.__class__.__name__}"
        )

    _grant_runtime_role_access(url)
    _migrate(url)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def app_engine(db_engine: Engine) -> Iterator[Engine]:
    """Return an engine bound to the same database as the runtime role."""
    engine = create_engine(
        _app_database_url(),
        pool_pre_ping=True,
        connect_args={"connect_timeout": _CONNECT_TIMEOUT_SECONDS},
    )
    yield engine
    engine.dispose()


def _transactional_session(engine: Engine) -> Iterator[Session]:
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def db_session(db_engine: Engine) -> Iterator[Session]:
    """Return an owner session whose changes are rolled back after each test."""
    yield from _transactional_session(db_engine)


@pytest.fixture
def app_session(app_engine: Engine) -> Iterator[Session]:
    """Return a runtime-role session whose changes are rolled back after each test."""
    yield from _transactional_session(app_engine)


@pytest.fixture(scope="session")
def redis_client() -> Iterator[redis.Redis]:
    """Return a Redis client bound to the test database index.

    Skips locally when Redis is not running, but fails in CI for the same
    reason the database fixture does.
    """
    from app.shared.cache import get_redis

    client = get_redis()
    try:
        client.ping()
    except redis.RedisError as exc:
        if os.environ.get("CI"):
            raise
        pytest.skip(f"Redis is not reachable: {exc.__class__.__name__}")

    # Index 15 is reserved for the suite; start from a clean slate.
    client.flushdb()
    yield client
    client.flushdb()


@pytest.fixture
def client() -> Iterator[TestClient]:
    """Return a test client bound to a freshly built application."""
    from app.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def api_client(app_session: Session) -> Iterator[TestClient]:
    """Return a client whose requests run against the test database.

    Bound to the runtime role, the same one the deployed application uses, so
    endpoint tests exercise the real privilege set.
    """
    from app.main import create_app
    from app.shared.db import get_session

    app = create_app()
    app.dependency_overrides[get_session] = lambda: app_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
