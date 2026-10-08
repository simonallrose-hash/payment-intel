"""SQLAlchemy 2.0 engine and session factory for PostgreSQL 16 (AS-16)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from payintel.core.settings import get_settings


def make_engine(dsn: str, *, pool_size: int = 10, statement_timeout_ms: int = 30_000) -> Engine:
    engine = create_engine(dsn, pool_size=pool_size, pool_pre_ping=True, future=True)

    @event.listens_for(engine, "connect")
    def _set_timeouts(dbapi_connection: object, _record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute(f"SET statement_timeout = {int(statement_timeout_ms)}")
        cursor.execute("SET timezone = 'UTC'")
        cursor.close()

    return engine


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    s = get_settings().postgres
    return make_engine(s.dsn, pool_size=s.pool_size, statement_timeout_ms=s.statement_timeout_ms)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    """Transactional scope: commit on success, rollback on error."""
    factory = make_session_factory(engine or get_engine())
    session = factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
