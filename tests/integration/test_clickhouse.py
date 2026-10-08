"""ClickHouse DDL applies idempotently and matches 5.2 (engines, keys, TTL)."""

from __future__ import annotations

import uuid
from datetime import date, datetime

import pytest
from clickhouse_connect.driver.client import Client

from payintel.core.ch import (
    MIGRATIONS_DIR,
    applied_versions,
    apply_migrations,
    discover_migrations,
    drop_all,
)

pytestmark = pytest.mark.integration


def test_apply_is_idempotent(ch_migrated: Client) -> None:
    assert apply_migrations(ch_migrated) == []
    assert applied_versions(ch_migrated) == {m.version for m in discover_migrations(MIGRATIONS_DIR)}


def test_tables_and_engines(ch_migrated: Client) -> None:
    rows = ch_migrated.query(
        "SELECT name, engine, partition_key, sorting_key FROM system.tables "
        "WHERE database = currentDatabase()"
    ).result_rows
    info = {r[0]: r[1:] for r in rows}
    assert info["obs_provider"] == (
        "MergeTree",
        "toYYYYMM(scan_date)",
        "provider_id, host_id, scan_ts",
    )
    assert info["obs_checkout_host"][2] == "third_party_etld1, host_id, scan_ts"
    assert info["obs_scan_stop"][2] == "stop_reason, platform_id, scan_ts"
    assert info["mv_market_share_monthly"][0] == "AggregatingMergeTree"
    assert info["mv_market_share_monthly_mv"][0] == "MaterializedView"


def test_ttl_clauses(ch_migrated: Client) -> None:
    ddl = {
        name: ch_migrated.command(f"SHOW CREATE TABLE {name}")
        for name in ("obs_provider", "obs_scan", "obs_scan_stop")
    }
    assert "toIntervalMonth(36)" in str(ddl["obs_provider"])
    assert "toIntervalMonth(12)" in str(ddl["obs_scan"])
    assert "toIntervalMonth(24)" in str(ddl["obs_scan_stop"])


def test_market_share_view_aggregates(ch_client: Client) -> None:
    ch_client.insert(
        "obs_provider",
        [
            [
                date(2026, 10, 1),
                datetime(2026, 10, 1, 10, 0, 0),
                1,
                "a.example",
                "stripe",
                "gateway",
                "network_host",
                "api.stripe.com",
                "checkout",
                "",
                "provider.stripe.network_host.2",
                1,
                "high",
                0.9,
                1,
                "",
                uuid.UUID("00000000-0000-0000-0000-000000000001"),
                "DE",
                "shopify",
                "fashion",
            ],
            [
                date(2026, 10, 2),
                datetime(2026, 10, 2, 10, 0, 0),
                1,
                "a.example",
                "stripe",
                "gateway",
                "js_global",
                "Stripe",
                "checkout",
                "",
                "provider.stripe.js_global.1",
                1,
                "high",
                0.9,
                1,
                "",
                uuid.UUID("00000000-0000-0000-0000-000000000002"),
                "DE",
                "shopify",
                "fashion",
            ],
            [
                date(2026, 10, 3),
                datetime(2026, 10, 3, 10, 0, 0),
                2,
                "b.example",
                "stripe",
                "gateway",
                "network_host",
                "api.stripe.com",
                "checkout",
                "",
                "provider.stripe.network_host.2",
                1,
                "high",
                0.9,
                1,
                "",
                uuid.UUID("00000000-0000-0000-0000-000000000003"),
                "DE",
                "shopify",
                "fashion",
            ],
        ],
        column_names=[
            "scan_date",
            "scan_ts",
            "host_id",
            "etld1",
            "provider_id",
            "role",
            "signal_type",
            "signal_value",
            "page_type",
            "page_url",
            "rule_id",
            "rule_version",
            "confidence",
            "confidence_score",
            "active_on_checkout",
            "evidence_key",
            "scan_run_id",
            "country",
            "platform_id",
            "vertical_id",
        ],
    )
    rows = ch_client.query(
        "SELECT month, country, provider_id, uniqMerge(stores) FROM mv_market_share_monthly "
        "GROUP BY month, country, provider_id"
    ).result_rows
    assert len(rows) == 1
    assert rows[0][1:] == ("DE", "stripe", 2)  # two distinct stores, three observations


def test_drop_all_and_reapply(ch_settings) -> None:  # type: ignore[no-untyped-def]
    """Reversibility on a disposable database (NFR-M-04)."""
    from payintel.core.ch import make_ch_client

    admin = make_ch_client(ch_settings, database="default")
    admin.command("CREATE DATABASE IF NOT EXISTS payintel_migr_test")
    client = make_ch_client(ch_settings, database="payintel_migr_test")
    try:
        assert len(apply_migrations(client)) == len(discover_migrations(MIGRATIONS_DIR))
        drop_all(client)
        assert client.query("SHOW TABLES").result_rows == []
        assert len(apply_migrations(client)) == len(discover_migrations(MIGRATIONS_DIR))
    finally:
        client.close()
        admin.command("DROP DATABASE IF EXISTS payintel_migr_test")
        admin.close()
