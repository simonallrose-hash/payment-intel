"""Postgres migrations apply on a clean database, roll back, and match the models (NFR-M-04)."""

from __future__ import annotations

import pytest
from alembic import command as alembic_command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, create_engine, inspect, text

from payintel.core.models import Base
from tests.conftest import alembic_config_for, include_object

pytestmark = pytest.mark.integration

EXPECTED_TABLES = {
    "domain",
    "host",
    "domain_source",
    "scan_plan",
    "scan_run",
    "store_account",
    "store_profile",
    "store_provider",
    "store_payment_method",
    "change_event",
    "provider",
    "payment_method",
    "platform",
    "vertical",
    "detection_rule",
    "gold_label",
    "organization",
    "kyc_record",
    "contract",
    "entitlement",
    "user_account",
    "membership",
    "api_key",
    "watchlist",
    "watchlist_item",
    "alert_rule",
    "webhook",
    "delivery",
    "export_job",
    "canary",
    "usage_log",
    "audit_log",
    "feature_flag",
    "optout_request",
}


def _tables(engine: Engine) -> set[str]:
    return {t for t in inspect(engine).get_table_names() if not t.startswith("usage_log_")}


def test_upgrade_downgrade_upgrade(fresh_database: str) -> None:
    cfg = alembic_config_for(fresh_database)
    engine = create_engine(fresh_database, future=True)
    try:
        alembic_command.upgrade(cfg, "head")
        assert EXPECTED_TABLES <= _tables(engine)
        alembic_command.downgrade(cfg, "base")
        assert _tables(engine) <= {"alembic_version"}
        alembic_command.upgrade(cfg, "head")
        assert EXPECTED_TABLES <= _tables(engine)
    finally:
        engine.dispose()


def test_models_and_migrations_agree(migrated_engine: Engine) -> None:
    """`alembic check` equivalent: no pending autogenerate diff."""
    with migrated_engine.connect() as conn:
        ctx = MigrationContext.configure(
            conn, opts={"compare_type": True, "include_object": include_object}
        )
        diff = compare_metadata(ctx, Base.metadata)
    assert diff == [], diff


def test_usage_log_is_partitioned(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as conn:
        partitions = (
            conn.execute(
                text(
                    "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
                    "JOIN pg_class p ON p.oid = i.inhparent WHERE p.relname = 'usage_log'"
                )
            )
            .scalars()
            .all()
        )
    assert "usage_log_default" in partitions
    assert "usage_log_y2026m10" in partitions
    assert len(partitions) >= 13


def test_audit_log_is_append_only(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as conn:
        tx = conn.begin()
        conn.execute(
            text(
                "INSERT INTO audit_log"
                "(actor, action, object_type, object_id, ts, prev_hash, row_hash) "
                "VALUES ('t', 'a', 'o', '1', now(), 'p', 'r-' || gen_random_uuid()::text)"
            )
        )
        for stmt in (
            "UPDATE audit_log SET actor = 'x' WHERE actor = 't'",
            "DELETE FROM audit_log WHERE actor = 't'",
        ):
            with pytest.raises(Exception, match="append-only"), conn.begin_nested():
                conn.execute(text(stmt))
        tx.rollback()
