"""Stage-4 tests reuse the stage-3 application and world fixtures, plus a CLI runner
bound to a fresh, migrated and seeded database."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from typer.testing import CliRunner

from payintel.core.settings import ClickHouseSettings, S3Settings
from tests.stage3.conftest import (  # noqa: F401 - re-exported fixtures
    app,
    app_state,
    client,
    export_store,
    test_settings,
    world,
)


@pytest.fixture
def cli_runner(
    fresh_database: str,
    ch_settings: ClickHouseSettings,
    s3_settings: S3Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[CliRunner]:
    from payintel.cli import app as cli_app
    from payintel.core.db import get_engine
    from payintel.core.settings import get_settings

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
        "PAYINTEL_SECRETS__ENCRYPTION_KEY": "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE=",
        "PAYINTEL_SECRETS__API_KEY_PEPPER": "p",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("PAYINTEL_ALEMBIC_DSN", raising=False)
    get_settings.cache_clear()
    get_engine.cache_clear()
    runner = CliRunner()
    try:
        for args in (["migrate"], ["seed"]):
            result = runner.invoke(cli_app, args)
            assert result.exit_code == 0, (args, result.output, result.exception)
        yield runner
    finally:
        get_engine().dispose()
        get_engine.cache_clear()
        get_settings.cache_clear()
