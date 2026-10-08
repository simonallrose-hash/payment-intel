"""Gold-set import, findings import and `make eval` metrics (FR-QA-01, FR-QA-02, AC-03)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from clickhouse_connect.driver.client import Client
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from payintel.cli import app
from payintel.core.clock import FixedClock
from payintel.core.errors import ValidationError
from payintel.core.models.base import ConfidenceLevel
from payintel.core.reference_loader import load_reference, sync_reference
from payintel.quality import eval as eval_mod
from payintel.quality.findings import import_findings_csv
from payintel.quality.gold import gold_size, import_gold_csv

pytestmark = pytest.mark.integration
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "gold"


@pytest.fixture
def seeded(db_session: Session, fixed_clock: FixedClock) -> Session:
    sync_reference(db_session, load_reference(), clock=fixed_clock)
    return db_session


def test_gold_import_upsert(seeded: Session, fixed_clock: FixedClock) -> None:
    r1 = import_gold_csv(
        seeded, FIXTURES / "sample_labels.csv", default_labeled_by="ci", clock=fixed_clock
    )
    assert r1.hosts_created == 12 and r1.inserted == 74 and r1.updated == 0
    assert gold_size(seeded) == 12
    r2 = import_gold_csv(
        seeded, FIXTURES / "sample_labels.csv", default_labeled_by="ci", clock=fixed_clock
    )
    assert (r2.inserted, r2.updated, r2.hosts_created) == (0, 74, 0)


def test_gold_import_rejects_unknown_entities(seeded: Session, tmp_path: Path) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text("domain,entity_type,entity_id,present\nshop.example,provider,not_a_psp,true\n")
    with pytest.raises(ValidationError, match="FR-NR-05"):
        import_gold_csv(seeded, bad, default_labeled_by="x")
    bad.write_text("domain,entity_type,entity_id,present\nshop.example,country,Germany,true\n")
    with pytest.raises(ValidationError, match="ISO"):
        import_gold_csv(seeded, bad, default_labeled_by="x")
    bad.write_text("domain,entity_type,entity_id\nshop.example,country,DE\n")
    with pytest.raises(ValidationError, match="missing columns"):
        import_gold_csv(seeded, bad, default_labeled_by="x")


def test_eval_metrics_on_sample(
    seeded: Session, ch_client: Client, fixed_clock: FixedClock
) -> None:
    import_gold_csv(
        seeded, FIXTURES / "sample_labels.csv", default_labeled_by="ci", clock=fixed_clock
    )
    findings = import_findings_csv(
        seeded, FIXTURES / "sample_findings.csv", ch=ch_client, clock=fixed_clock
    )
    assert findings.observations == 48
    report = eval_mod.evaluate(seeded, ch=ch_client)
    assert report.gold_hosts == 12
    p = report.provider.overall
    assert (p.tp, p.fp, p.fn) == (16, 0, 1)  # worldline on shop-j is the designed miss
    assert p.precision == 1.0 and round(p.recall or 0, 3) == 0.941
    m = report.payment_method.overall
    assert (m.tp, m.fp, m.fn) == (31, 1, 0)  # google_pay on shop-i is the designed FP
    assert report.platform.overall.fp == 1  # shop-k predicted shopify, labelled custom
    assert report.country.overall.precision == 1.0
    assert "provider.stripe.network_host.1" in report.provider.per_rule
    # a rule that never fired has no per-rule entry; the miss is counted in overall.fn
    assert "provider.worldline.network_host.1" not in report.provider.per_rule
    assert eval_mod.gate(report, min_psp_precision=0.95).passed
    # stricter confidence filter drops medium-confidence method predictions
    strict = eval_mod.evaluate(seeded, ch=None, min_confidence=ConfidenceLevel.HIGH)
    assert strict.payment_method.overall.tp == 0 and strict.payment_method.overall.fn == 31
    assert any("per-rule" in n for n in strict.notes)


def test_eval_gate_fails_on_false_positives(
    seeded: Session, fixed_clock: FixedClock, tmp_path: Path
) -> None:
    import_gold_csv(
        seeded, FIXTURES / "sample_labels.csv", default_labeled_by="ci", clock=fixed_clock
    )
    noisy = tmp_path / "noisy.csv"
    noisy.write_text(
        "domain,entity_type,entity_id,confidence\n"
        "shop-a.example,provider,stripe,high\n"
        "shop-a.example,provider,adyen,high\n"  # labelled absent
        "shop-b.example,provider,mollie,high\n"  # not labelled present
    )
    import_findings_csv(seeded, noisy, clock=fixed_clock)
    report = eval_mod.evaluate(seeded)
    assert report.provider.overall.precision == pytest.approx(1 / 3)
    verdict = eval_mod.gate(report, min_psp_precision=0.95)
    assert not verdict.passed and "release blocked" in verdict.reason


def test_cli_rules_check_and_eval_report(
    tmp_path: Path, pg_dsn: str, ch_settings, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    runner = CliRunner()
    result = runner.invoke(app, ["rules-check"])
    assert result.exit_code == 0, result.output
    assert "ok:" in result.output and "needs_verification:" in result.output
    # eval against the migrated database (empty gold set → gate not applicable, exit 0)
    monkeypatch.setenv("PAYINTEL_POSTGRES__DSN", pg_dsn)
    monkeypatch.setenv("PAYINTEL_CLICKHOUSE__URL", ch_settings.url)
    monkeypatch.setenv("PAYINTEL_CLICKHOUSE__USER", ch_settings.user)
    monkeypatch.setenv("PAYINTEL_CLICKHOUSE__PASSWORD", ch_settings.password.get_secret_value())
    monkeypatch.setenv("PAYINTEL_CLICKHOUSE__DATABASE", ch_settings.database)
    from payintel.core.db import get_engine
    from payintel.core.settings import get_settings

    get_settings.cache_clear()
    get_engine.cache_clear()
    report = tmp_path / "eval.json"
    result = runner.invoke(app, ["eval", "--report", str(report)])
    assert result.exit_code == 0, result.output
    payload = json.loads(report.read_text())
    assert payload["gate"]["passed"] is True and payload["gate"]["threshold"] == 0.95
    get_engine.cache_clear()


def test_cli_end_to_end(tmp_path: Path, fresh_database: str, ch_settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """migrate → seed → gold import → import-findings → eval → flags → audit verify."""
    monkeypatch.setenv("PAYINTEL_POSTGRES__DSN", fresh_database)
    monkeypatch.setenv("PAYINTEL_CLICKHOUSE__URL", ch_settings.url)
    monkeypatch.setenv("PAYINTEL_CLICKHOUSE__USER", ch_settings.user)
    monkeypatch.setenv("PAYINTEL_CLICKHOUSE__PASSWORD", ch_settings.password.get_secret_value())
    monkeypatch.setenv("PAYINTEL_CLICKHOUSE__DATABASE", ch_settings.database)
    monkeypatch.delenv("PAYINTEL_ALEMBIC_DSN", raising=False)
    from payintel.core.db import get_engine
    from payintel.core.settings import get_settings

    get_settings.cache_clear()
    get_engine.cache_clear()
    runner = CliRunner()
    try:
        steps = [
            (["migrate"], "postgres: migrated to head"),
            (["seed"], "reference: +136 ~0 =0; rules: +268"),
            (["seed"], "reference: +0 ~0 =136; rules: +0 ~0 =268"),
            (["gold", "import", str(FIXTURES / "sample_labels.csv"), "--labeled-by", "ci"], "+74"),
            (["gold", "import-findings", str(FIXTURES / "sample_findings.csv")], "observations 48"),
            (["eval", "--report", str(tmp_path / "r.json")], "gate: PASS"),
            (["flags", "list"], "feature_c2_enabled = off"),
            (["flags", "set", "feature_c2_enabled", "on", "--actor", "test"], "= on"),
            (["flags", "list"], "feature_c2_enabled = on"),
            (["audit", "verify"], "audit chain ok"),
        ]
        for args, expected in steps:
            result = runner.invoke(app, args)
            assert result.exit_code == 0, (args, result.output)
            assert expected in result.output, (args, result.output)
        payload = json.loads((tmp_path / "r.json").read_text())
        assert payload["provider"]["overall"]["precision"] == 1.0
        # the strict gate must fail: provider recall is below 1 but precision is 1,
        # so raise the precision bar above 1 to force the exit code path
        result = runner.invoke(app, ["eval", "--min-psp-precision", "1.01", "--no-clickhouse"])
        assert result.exit_code == 1 and "gate: FAIL" in result.output
        result = runner.invoke(app, ["flags", "set", "no_such_flag", "on"])
        assert result.exit_code != 0
    finally:
        get_engine().dispose()
        get_engine.cache_clear()
        get_settings.cache_clear()
