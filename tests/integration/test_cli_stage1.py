"""Stage 1 CLI: discovery import/resolve/update-psl, scheduler plan/prioritize, worker-light.

Runs against a throw-away database; DNS points at a closed local port so every
lookup fails fast (no network, pytest-socket), which is enough to prove wiring.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from payintel.cli import app
from payintel.core.settings import ClickHouseSettings, S3Settings
from payintel.discovery.psl import PSL_PATH

pytestmark = pytest.mark.integration
SOURCES = Path(__file__).resolve().parents[1] / "fixtures" / "sources"


def test_cli_stage1_commands(
    tmp_path: Path,
    fresh_database: str,
    ch_settings: ClickHouseSettings,
    s3_settings: S3Settings,
    redis_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = {
        "PAYINTEL_POSTGRES__DSN": fresh_database,
        "PAYINTEL_CLICKHOUSE__URL": ch_settings.url,
        "PAYINTEL_CLICKHOUSE__USER": ch_settings.user,
        "PAYINTEL_CLICKHOUSE__PASSWORD": ch_settings.password.get_secret_value(),
        "PAYINTEL_CLICKHOUSE__DATABASE": ch_settings.database,
        "PAYINTEL_S3__ENDPOINT": s3_settings.endpoint,
        "PAYINTEL_S3__ACCESS_KEY": s3_settings.access_key.get_secret_value(),
        "PAYINTEL_S3__SECRET_KEY": s3_settings.secret_key.get_secret_value(),
        "PAYINTEL_S3__BUCKET_ARTIFACTS": s3_settings.bucket_artifacts,
        "PAYINTEL_REDIS__URL": redis_url,
        "PAYINTEL_DISCOVERY__DNS_PORT": "1",  # nothing listens: every lookup is an error
        "PAYINTEL_DISCOVERY__DNS_TIMEOUT_SECONDS": "0.2",
        "PAYINTEL_LIGHT__CONNECT_TIMEOUT_SECONDS": "0.5",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("PAYINTEL_ALEMBIC_DSN", raising=False)
    from payintel.core.db import get_engine
    from payintel.core.settings import get_settings

    get_settings.cache_clear()
    get_engine.cache_clear()
    runner = CliRunner()
    psl_copy = tmp_path / "psl.dat"
    psl_copy.write_bytes(PSL_PATH.read_bytes())
    bad_psl = tmp_path / "bad.dat"
    bad_psl.write_text("// not a suffix list\ncom\n", encoding="utf-8")
    try:
        steps = [
            (["migrate"], "postgres: migrated to head"),
            (["seed"], "reference: +136"),
            (
                ["discovery", "import", str(SOURCES / "tranco_sample.csv"), "--source", "tranco"],
                "tranco: batch tranco:",
            ),
            (
                ["discovery", "import", str(SOURCES / "manual_sample.csv")],
                "manual: batch manual:",
            ),
            (["discovery", "resolve", "--limit", "2"], "dns: checked 2, ok 0, no_dns 0, errors 2"),
            (["scheduler", "plan"], "light: plans +"),
            (["scheduler", "plan", "--scan-type", "checkout"], "checkout: plans +"),
            (
                ["scheduler", "prioritize", "brand.de", "--actor", "test"],
                "plans prioritised (audited as test)",
            ),
            (["discovery", "update-psl", str(psl_copy)], "rules written to"),
            (["worker-light", "--once", "--limit", "3", "--concurrency", "3"], "batch: 3 scans"),
            (["audit", "verify"], "audit chain ok"),
        ]
        for args, expected in steps:
            result = runner.invoke(app, args)
            assert result.exit_code == 0, (args, result.output, result.exception)
            assert expected in result.output, (args, result.output)
        # CZDS stays off until the flag is on (AS-22)
        result = runner.invoke(
            app, ["discovery", "import", str(SOURCES / "czds_sample.zone"), "--source", "czds"]
        )
        assert result.exit_code != 0
        result = runner.invoke(app, ["discovery", "update-psl", str(bad_psl)])
        assert result.exit_code != 0
        assert PSL_PATH.read_bytes() == psl_copy.read_bytes()  # untouched by the bad file
    finally:
        get_engine().dispose()
        get_engine.cache_clear()
        get_settings.cache_clear()
        for k in env:
            os.environ.pop(k, None)
