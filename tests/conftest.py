"""Shared fixtures: deterministic clock, Postgres / ClickHouse / S3 via testcontainers.

Brief rule 9: tests never reach the internet (pytest-socket allows only
localhost), never depend on wall time (FixedClock) and are order-independent
(each DB test runs inside a transaction that is rolled back; ClickHouse tables
are truncated per test).

External services can be supplied instead of containers (CI service jobs, or a
developer with `make dev` running) through:
  PAYINTEL_TEST_PG_DSN, PAYINTEL_TEST_CH_URL (+ _CH_USER/_CH_PASSWORD/_CH_DATABASE),
  PAYINTEL_TEST_S3_ENDPOINT (+ _S3_ACCESS_KEY/_S3_SECRET_KEY).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from clickhouse_connect.driver.client import Client
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from payintel.core.ch import apply_migrations, make_ch_client
from payintel.core.clock import FixedClock
from payintel.core.models import Base
from payintel.core.s3 import ObjectStore, make_s3_client
from payintel.core.settings import ClickHouseSettings, S3Settings, get_settings

ROOT = Path(__file__).resolve().parents[1]
PG_IMAGE = os.environ.get("PAYINTEL_TEST_PG_IMAGE", "postgres:16-alpine")
CH_IMAGE = os.environ.get("PAYINTEL_TEST_CH_IMAGE", "clickhouse/clickhouse-server:24.8")
S3_IMAGE = os.environ.get("PAYINTEL_TEST_S3_IMAGE", "minio/minio:RELEASE.2024-12-18T13-15-44Z")

FIXED_NOW = datetime(2026, 10, 8, 12, 0, 0, tzinfo=UTC)


@pytest_asyncio.fixture(scope="session", loop_scope="session", autouse=True)
async def session_loop() -> asyncio.AbstractEventLoop:
    """The pytest-asyncio session loop shared by the browser tests."""
    return asyncio.get_running_loop()


@pytest.fixture(autouse=True)
def _restore_session_loop(session_loop: asyncio.AbstractEventLoop) -> Iterator[None]:
    """Keep the session loop current.

    ``asyncio.run`` (used by the CLI commands under test) clears the current
    event loop on exit; without this, every ``loop_scope="session"`` test that
    runs after such a test fails with "There is no current event loop".
    """
    if not session_loop.is_closed():
        asyncio.set_event_loop(session_loop)
    yield


@pytest.fixture
def fixed_clock() -> FixedClock:
    return FixedClock(FIXED_NOW)


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --- Postgres ---------------------------------------------------------------


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    external = os.environ.get("PAYINTEL_TEST_PG_DSN")
    if external:
        yield external
        return
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer(
        PG_IMAGE, driver="psycopg", username="t", password="t", dbname="t"
    ) as pg:
        yield pg.get_connection_url()


def include_object(obj: object, name: str | None, type_: str, *_: object) -> bool:
    """Same filter as alembic/env.py: ignore usage_log partition children."""
    return not (
        type_ in {"table", "index"}
        and name is not None
        and name.startswith(("usage_log_y", "usage_log_default"))
    )


def alembic_config_for(dsn: str) -> AlembicConfig:
    cfg = AlembicConfig(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    os.environ["PAYINTEL_ALEMBIC_DSN"] = dsn
    return cfg


@pytest.fixture(scope="session")
def migrated_engine(pg_dsn: str) -> Iterator[Engine]:
    """Engine on a database reset to an empty head schema once per session."""
    cfg = alembic_config_for(pg_dsn)
    alembic_command.downgrade(cfg, "base")
    alembic_command.upgrade(cfg, "head")
    engine = create_engine(pg_dsn, future=True)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(migrated_engine: Engine) -> Iterator[Session]:
    """Session inside an outer transaction that is always rolled back."""
    connection = migrated_engine.connect()
    outer = connection.begin()
    session = sessionmaker(
        bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )()
    try:
        yield session
    finally:
        session.close()
        outer.rollback()
        connection.close()


@pytest.fixture
def fresh_database(pg_dsn: str) -> Iterator[str]:
    """A brand-new empty database in the same server, for migration up/down tests."""
    name = f"migr_{os.getpid()}_{abs(hash(pg_dsn)) % 10_000}"
    admin = create_engine(pg_dsn, isolation_level="AUTOCOMMIT", future=True)
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    dsn = pg_dsn.rsplit("/", 1)[0] + f"/{name}"
    try:
        yield dsn
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


# --- ClickHouse -------------------------------------------------------------


@pytest.fixture(scope="session")
def ch_settings() -> Iterator[ClickHouseSettings]:
    external = os.environ.get("PAYINTEL_TEST_CH_URL")
    if external:
        yield ClickHouseSettings(
            url=external,
            user=os.environ.get("PAYINTEL_TEST_CH_USER", "default"),
            password=os.environ.get("PAYINTEL_TEST_CH_PASSWORD", ""),
            database=os.environ.get("PAYINTEL_TEST_CH_DATABASE", "default"),
        )
        return
    from testcontainers.community.clickhouse import ClickHouseContainer

    container = ClickHouseContainer(
        CH_IMAGE, username="t", password="t", dbname="t"
    ).with_exposed_ports(8123)
    with container as ch:
        port = ch.get_exposed_port(8123)
        yield ClickHouseSettings(
            url=f"http://127.0.0.1:{port}", user="t", password="t", database="t"
        )


@pytest.fixture(scope="session")
def ch_migrated(ch_settings: ClickHouseSettings) -> Iterator[Client]:
    client = make_ch_client(ch_settings)
    apply_migrations(client)
    yield client
    client.close()


@pytest.fixture
def ch_client(ch_migrated: Client) -> Iterator[Client]:
    """Migrated ClickHouse with observation tables truncated before each test."""
    for table in (
        "obs_provider",
        "obs_payment_method",
        "obs_checkout_host",
        "obs_tech",
        "obs_scan",
        "obs_scan_stop",
        "mv_market_share_monthly",
    ):
        ch_migrated.command(f"TRUNCATE TABLE IF EXISTS {table}")
    yield ch_migrated


# --- S3 ---------------------------------------------------------------------


@pytest.fixture(scope="session")
def s3_settings() -> Iterator[S3Settings]:
    external = os.environ.get("PAYINTEL_TEST_S3_ENDPOINT")
    if external:
        yield S3Settings(
            endpoint=external,
            access_key=os.environ.get("PAYINTEL_TEST_S3_ACCESS_KEY", "test"),
            secret_key=os.environ.get("PAYINTEL_TEST_S3_SECRET_KEY", "testtest"),
            bucket_artifacts="payintel-test-artifacts",
            bucket_exports="payintel-test-exports",
        )
        return
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.wait_strategies import HttpWaitStrategy

    container = (
        DockerContainer(S3_IMAGE)
        .with_env("MINIO_ROOT_USER", "testtest")
        .with_env("MINIO_ROOT_PASSWORD", "testtesttest")
        .with_command("server /data")
        .with_exposed_ports(9000)
        .waiting_for(HttpWaitStrategy(9000).for_path("/minio/health/live"))
    )
    with container as minio:
        port = minio.get_exposed_port(9000)
        yield S3Settings(
            endpoint=f"http://127.0.0.1:{port}",
            access_key="testtest",
            secret_key="testtesttest",
            bucket_artifacts="payintel-test-artifacts",
            bucket_exports="payintel-test-exports",
        )


@pytest.fixture
def object_store(s3_settings: S3Settings) -> ObjectStore:
    store = ObjectStore(make_s3_client(s3_settings))
    store.ensure_bucket(s3_settings.bucket_artifacts)
    return store


@pytest.fixture
def metadata_tables() -> list[str]:
    return sorted(Base.metadata.tables.keys())


# --- Redis --------------------------------------------------------------------


@pytest.fixture(scope="session")
def redis_url() -> Iterator[str]:
    external = os.environ.get("PAYINTEL_TEST_REDIS_URL")
    if external:
        yield external
        return
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.wait_strategies import LogMessageWaitStrategy

    container = (
        DockerContainer(os.environ.get("PAYINTEL_TEST_REDIS_IMAGE", "redis:7-alpine"))
        .with_exposed_ports(6379)
        .waiting_for(LogMessageWaitStrategy("Ready to accept connections"))
    )
    with container as redis:
        yield f"redis://127.0.0.1:{redis.get_exposed_port(6379)}/0"
