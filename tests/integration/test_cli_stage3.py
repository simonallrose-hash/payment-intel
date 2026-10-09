"""Stage-3 operator commands end to end on a fresh database: organisations,
users, API keys, alert/export workers, reports, opt-out and DSAR listings."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from payintel.cli import app
from payintel.core.settings import ClickHouseSettings, S3Settings

pytestmark = pytest.mark.integration


def test_cli_stage3_commands(
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
        "PAYINTEL_S3__BUCKET_EXPORTS": s3_settings.bucket_exports,
        "PAYINTEL_REDIS__URL": redis_url,
        "PAYINTEL_SECRETS__ENCRYPTION_KEY": "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE=",
        "PAYINTEL_SECRETS__API_KEY_PEPPER": "cli-test-pepper",
        "PAYINTEL_DISCOVERY__DNS_PORT": "1",
        "PAYINTEL_DISCOVERY__DNS_TIMEOUT_SECONDS": "0.2",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("PAYINTEL_ALEMBIC_DSN", raising=False)
    from payintel.core.db import get_engine
    from payintel.core.settings import get_settings

    get_settings.cache_clear()
    get_engine.cache_clear()
    runner = CliRunner()
    try:
        for args in (["migrate"], ["seed"]):
            result = runner.invoke(app, args)
            assert result.exit_code == 0, (args, result.output, result.exception)

        result = runner.invoke(
            app, ["orgs", "create", "--legal-name", "CLI GmbH", "--country", "de"]
        )
        assert result.exit_code == 0, (result.output, result.exception)
        org_id = result.output.strip().splitlines()[-1]
        assert re.fullmatch(r"[0-9a-f-]{36}", org_id)
        result = runner.invoke(app, ["orgs", "list"])
        assert result.exit_code == 0 and "applied" in result.output and "CLI GmbH" in result.output

        result = runner.invoke(
            app,
            ["users", "create", "--email", "ops@payintel.test", "--role", "staff_admin"],
            input="correct horse battery staple 42\n",
        )
        assert result.exit_code == 0, (result.output, result.exception)
        assert "ops@payintel.test staff_admin" in result.output
        result = runner.invoke(
            app,
            ["users", "create", "--email", "ops@payintel.test", "--role", "staff_admin"],
            input="correct horse battery staple 42\n",
        )
        assert result.exit_code != 0  # duplicate e-mail

        result = runner.invoke(
            app, ["keys", "issue", "--org", org_id, "--name", "ops", "--expires-in-days", "30"]
        )
        assert result.exit_code == 0, (result.output, result.exception)
        raw = result.output.strip().splitlines()[-1]
        assert raw.startswith("pik_") and "." in raw and len(raw) > 50
        monkeypatch.setenv("PAYINTEL_SECRETS__API_KEY_PEPPER", "")
        get_settings.cache_clear()
        result = runner.invoke(app, ["keys", "issue", "--org", org_id, "--name", "no-pepper"])
        assert result.exit_code == 2
        monkeypatch.setenv("PAYINTEL_SECRETS__API_KEY_PEPPER", "cli-test-pepper")
        get_settings.cache_clear()

        result = runner.invoke(app, ["alerts", "dispatch", "--once"])
        assert result.exit_code == 0, (result.output, result.exception)
        assert "new deliveries=0" in result.output
        result = runner.invoke(app, ["exports", "run", "--once"])
        assert result.exit_code == 0, (result.output, result.exception)
        assert "scheduled=0 built=0 failed=0" in result.output

        out = tmp_path / "reports"
        result = runner.invoke(app, ["report", "build", "--country", "de", "--out", str(out)])
        assert result.exit_code == 0, (result.output, result.exception)
        assert '"total_stores": 0' in result.output
        assert list(out.glob("payintel-report-*.xlsx")) and list(out.glob("*.csv.zip"))

        csv_out = tmp_path / "stops.csv"
        for args, expected in (
            (["optout", "verify-pending"], "verified 0, applied 0"),
            (["optout", "apply"], "applied 0"),
            (["dsar", "list"], ""),
            (["quality", "dashboard", "--json"], "{"),
            (["quality", "dashboard"], ""),
            (["quality", "stops", "--days", "7", "--json"], "{"),
            (["quality", "stops"], ""),
            (["quality", "stop-sample", "checkout", "checkout_not_found"], ""),
            (
                ["quality", "stop-sample", "checkout", "checkout_not_found", "--csv", str(csv_out)],
                "0 rows written",
            ),
            (["quality", "stop-alerts"], "new alert(s)"),
        ):
            result = runner.invoke(app, args)
            assert result.exit_code == 0, (args, result.output, result.exception)
            assert expected in result.output
    finally:
        get_engine().dispose()
        get_engine.cache_clear()
        get_settings.cache_clear()
        for k in env:
            os.environ.pop(k, None)


def test_api_serve_builds_the_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """`api serve` wires settings → state → app and hands it to uvicorn."""
    import uvicorn

    from payintel.api import runtime
    from payintel.core.settings import get_settings

    monkeypatch.setenv(
        "PAYINTEL_SECRETS__ENCRYPTION_KEY", "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE="
    )
    monkeypatch.setenv("PAYINTEL_SECRETS__API_KEY_PEPPER", "p")
    get_settings.cache_clear()
    captured: dict[str, object] = {}

    def fake_build_state(settings: object) -> object:
        from unittest.mock import MagicMock

        from payintel.api.deps import AppState
        from payintel.core.clock import SYSTEM_CLOCK
        from payintel.entitlements.quotas import MemoryCounterStore, RateLimiter

        return AppState(
            settings=get_settings(),
            session_factory=MagicMock(),
            clock=SYSTEM_CLOCK,
            limiter=RateLimiter(MemoryCounterStore()),
            store=None,
        )

    def fake_run(application: object, **kwargs: object) -> None:
        captured["app"] = application
        captured.update(kwargs)

    monkeypatch.setattr(runtime, "build_state", fake_build_state)
    monkeypatch.setattr(uvicorn, "run", fake_run)
    try:
        result = CliRunner().invoke(app, ["api", "serve", "--port", "8123", "--workers", "2"])
        assert result.exit_code == 0, (result.output, result.exception)
        assert captured["port"] == 8123 and captured["workers"] == 2
        assert captured["proxy_headers"] is True
        assert hasattr(captured["app"], "openapi")
    finally:
        get_settings.cache_clear()
