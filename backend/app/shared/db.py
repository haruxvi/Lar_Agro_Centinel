"""Database engine, session factory and declarative base."""

from __future__ import annotations

from collections.abc import Generator
from functools import lru_cache

from sqlalchemy import Dialect, Engine, create_engine
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import TypeDecorator

from app.shared.config import get_settings


class Base(DeclarativeBase):
    """Declarative base shared by every module's models."""


class IPAddress(TypeDecorator[str]):
    """An INET column that reads back as a string.

    psycopg returns ``ipaddress`` objects for INET, which do not match the
    ``str`` annotations on the models and are not what Pydantic expects when
    serialising. The database keeps its validation, the application keeps
    plain strings.
    """

    impl = INET
    cache_ok = True

    def process_result_value(self, value: object, dialect: Dialect) -> str | None:
        """Return the stored address as a string."""
        return None if value is None else str(value)


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Return the process-wide SQLAlchemy engine."""
    settings = get_settings()
    return create_engine(
        str(settings.database_url),
        pool_pre_ping=True,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        echo=settings.database_echo,
    )


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    """Return the process-wide session factory."""
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a scoped database session."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()
