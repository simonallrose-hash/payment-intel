"""Alembic environment: metadata from `payintel.core.models`, DSN from settings."""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from payintel.core.models import Base
from payintel.core.settings import get_settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Partition children of `usage_log` are created by DDL in the migration, not by
# the models; autogenerate must not try to drop their inherited indexes.
PARTITION_PREFIXES = ("usage_log_y", "usage_log_default")


def include_object(obj, name, type_, reflected, compare_to):  # type: ignore[no-untyped-def]
    if type_ in {"table", "index"} and name and name.startswith(PARTITION_PREFIXES):
        return False
    return True


def _dsn() -> str:
    return os.environ.get("PAYINTEL_ALEMBIC_DSN") or get_settings().postgres.dsn


def run_migrations_offline() -> None:
    context.configure(
        url=_dsn(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _dsn()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
