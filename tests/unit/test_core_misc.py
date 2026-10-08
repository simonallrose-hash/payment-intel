"""Crypto, clock, logging, ClickHouse migration parsing, partitions, coverage script."""

from __future__ import annotations

import base64
import io
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from payintel.core import logging as plog
from payintel.core.ch import MIGRATIONS_DIR, discover_migrations
from payintel.core.clock import FixedClock, SystemClock
from payintel.core.crypto import SecretBox, constant_time_equals, hash_api_key
from payintel.core.errors import ConfigurationError
from payintel.core.partitions import month_bounds, months_from, partition_ddl, partition_name


def test_secretbox_roundtrip() -> None:
    key = base64.b64encode(os.urandom(32)).decode()
    box = SecretBox.from_base64(key)
    token = box.encrypt("p@ssw0rd", associated_data="host:1")
    assert token.startswith("v1:")
    assert box.decrypt(token, associated_data="host:1") == "p@ssw0rd"
    with pytest.raises(Exception):  # noqa: B017 - any AEAD failure
        box.decrypt(token, associated_data="host:2")


def test_secretbox_requires_key() -> None:
    with pytest.raises(ConfigurationError):
        SecretBox.from_base64("")
    with pytest.raises(ConfigurationError):
        SecretBox.from_base64(base64.b64encode(b"short").decode())
    with pytest.raises(ConfigurationError):
        SecretBox.from_base64("not base64!!")


def test_api_key_hash_uses_pepper() -> None:
    a = hash_api_key("k", "pepper-a")
    b = hash_api_key("k", "pepper-b")
    assert a != b and len(a) == 64
    assert constant_time_equals(a, hash_api_key("k", "pepper-a"))
    with pytest.raises(ConfigurationError):
        hash_api_key("k", "")


def test_fixed_clock() -> None:
    c = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    c.advance(days=1, seconds=5)
    assert c.now() == datetime(2026, 1, 2, 0, 0, 5, tzinfo=UTC)
    with pytest.raises(ValueError):
        FixedClock(datetime(2026, 1, 1))
    assert SystemClock().now().tzinfo is not None


def test_logging_context_fields() -> None:
    buf = io.StringIO()
    plog.configure_logging("INFO", json_output=True, stream=buf)
    log = plog.get_logger("t")
    with plog.log_context(request_id="r1", scan_run_id="s1", org_id=None):
        log.info("hello", extra=1)
    record = json.loads(buf.getvalue().strip().splitlines()[-1])
    assert record["request_id"] == "r1" and record["scan_run_id"] == "s1"
    assert "org_id" not in record
    assert record["event"] == "hello" and record["logger"] == "t"


def test_clickhouse_migrations_discoverable() -> None:
    ms = discover_migrations(MIGRATIONS_DIR)
    assert [m.version for m in ms] == list(range(1, len(ms) + 1))
    names = {m.name for m in ms}
    assert {
        "obs_provider",
        "obs_payment_method",
        "obs_checkout_host",
        "obs_tech",
        "obs_scan",
        "obs_scan_stop",
        "mv_market_share_monthly",
    } <= names
    for m in ms:
        assert m.statements(), m.name
        assert all("IF NOT EXISTS" in s for s in m.statements()), m.name


def test_clickhouse_ttls_match_tz() -> None:
    text = {m.name: m.path.read_text() for m in discover_migrations(MIGRATIONS_DIR)}
    assert "INTERVAL 36 MONTH" in text["obs_provider"]
    assert "INTERVAL 12 MONTH" in text["obs_scan"]
    assert "INTERVAL 24 MONTH" in text["obs_scan_stop"]
    assert "ORDER BY (stop_reason, platform_id, scan_ts)" in text["obs_scan_stop"]
    assert "uniqState(host_id)" in text["mv_market_share_monthly"]


def test_bad_migration_filename(tmp_path: Path) -> None:
    (tmp_path / "x.sql").write_text("CREATE TABLE IF NOT EXISTS a (x Int8) ENGINE=Memory;")
    with pytest.raises(ConfigurationError):
        discover_migrations(tmp_path)


def test_partition_helpers() -> None:
    assert month_bounds(2026, 12) == (datetime(2026, 12, 1).date(), datetime(2027, 1, 1).date())
    assert partition_name(2026, 10) == "usage_log_y2026m10"
    assert "FROM ('2026-10-01') TO ('2026-11-01')" in partition_ddl(2026, 10)
    assert months_from(datetime(2026, 11, 1).date(), 3) == [(2026, 11), (2026, 12), (2027, 1)]


def test_check_coverage_script(tmp_path: Path) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "check_coverage", Path(__file__).resolve().parents[2] / "scripts" / "check_coverage.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    report = {
        "files": {
            "src/payintel/detect/rules.py": {
                "summary": {"covered_lines": 90, "num_statements": 100}
            },
            "src/payintel/core/db.py": {"summary": {"covered_lines": 75, "num_statements": 100}},
        }
    }
    p = tmp_path / "c.json"
    p.write_text(json.dumps(report))
    assert mod.main(["x", str(p)]) == 0
    report["files"]["src/payintel/detect/rules.py"]["summary"]["covered_lines"] = 80
    p.write_text(json.dumps(report))
    assert mod.main(["x", str(p)]) == 1
