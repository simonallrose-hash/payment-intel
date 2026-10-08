"""`payintel` command-line interface (Makefile targets wrap these commands)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig

from payintel.core import audit as audit_mod
from payintel.core.ch import apply_migrations, make_ch_client
from payintel.core.db import get_engine, session_scope
from payintel.core.flags import FlagService
from payintel.core.logging import configure_logging, get_logger
from payintel.core.models.base import ConfidenceLevel
from payintel.core.reference_loader import load_reference, sync_reference
from payintel.core.settings import get_settings
from payintel.detect.rules import load_rules, sync_rules
from payintel.quality import eval as eval_mod
from payintel.quality.findings import import_findings_csv
from payintel.quality.gold import gold_size, import_gold_csv

app = typer.Typer(no_args_is_help=True, add_completion=False, help="PayIntel operations CLI")
gold_app = typer.Typer(no_args_is_help=True, help="Gold set (FR-QA-01)")
flags_app = typer.Typer(no_args_is_help=True, help="Feature flags (FR-ADM-05)")
audit_app = typer.Typer(no_args_is_help=True, help="Audit log (FR-AB-01, NFR-S-11)")
app.add_typer(gold_app, name="gold")
app.add_typer(flags_app, name="flags")
app.add_typer(audit_app, name="audit")

ROOT = Path(__file__).resolve().parents[2]
log = get_logger("payintel.cli")


@app.callback()
def _root() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json_output=settings.environment != "dev")


def _alembic_config() -> AlembicConfig:
    cfg = AlembicConfig(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    return cfg


@app.command()
def migrate(
    postgres: Annotated[bool, typer.Option(help="Apply Alembic migrations")] = True,
    clickhouse: Annotated[bool, typer.Option(help="Apply ClickHouse migrations")] = True,
) -> None:
    """Apply Postgres (Alembic) and ClickHouse migrations (NFR-M-04)."""
    if postgres:
        alembic_command.upgrade(_alembic_config(), "head")
        typer.echo("postgres: migrated to head")
    if clickhouse:
        client = make_ch_client(get_settings().clickhouse)
        applied = apply_migrations(client)
        typer.echo(f"clickhouse: applied {[m.name for m in applied] or 'nothing (up to date)'}")


@app.command()
def seed(actor: Annotated[str, typer.Option(help="Audit actor")] = "seed") -> None:
    """Load reference dictionaries and detection rules into Postgres (FR-NR-*, FR-DT-01)."""
    reference = load_reference()
    ruleset = load_rules(reference=reference)
    with session_scope(get_engine()) as session:
        ref_result = sync_reference(session, reference, actor=actor)
        rule_result = sync_rules(session, ruleset)
    typer.echo(
        f"reference: +{ref_result.inserted} ~{ref_result.updated} ={ref_result.unchanged}; "
        f"rules: +{rule_result.inserted} ~{rule_result.updated} ={rule_result.unchanged} "
        f"(ruleset {ruleset.version}, {len(ruleset.enabled())} enabled, "
        f"{len(ruleset.needing_verification())} need verification)"
    )


@app.command("rules-check")
def rules_check() -> None:
    """Validate reference and rule files without touching the database."""
    reference = load_reference()
    ruleset = load_rules(reference=reference)
    typer.echo(f"ok: {len(ruleset.rules)} rules, ruleset version {ruleset.version}")
    for r in ruleset.needing_verification():
        typer.echo(f"needs_verification: {r.rule_id} ({r.signal_type.value} {r.pattern})")


@gold_app.command("import")
def gold_import(
    csv_path: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    labeled_by: Annotated[
        str, typer.Option(help="Default author for rows without labeled_by")
    ] = "analyst",
) -> None:
    """Import gold labels from CSV (upsert)."""
    with session_scope(get_engine()) as session:
        result = import_gold_csv(session, csv_path, default_labeled_by=labeled_by)
        size = gold_size(session)
    typer.echo(
        f"gold: +{result.inserted} ~{result.updated}, hosts created {result.hosts_created}, "
        f"labelled hosts total {size} (target {get_settings().quality.gold_set_target_size})"
    )


@gold_app.command("import-findings")
def gold_import_findings(
    csv_path: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    clickhouse: Annotated[
        bool, typer.Option(help="Also write obs_* rows for per-rule metrics")
    ] = True,
) -> None:
    """Import detector output from CSV into store_* (and obs_* when ClickHouse is enabled)."""
    ch = make_ch_client(get_settings().clickhouse) if clickhouse else None
    with session_scope(get_engine()) as session:
        result = import_findings_csv(session, csv_path, ch=ch)
    typer.echo(
        f"findings: providers {result.providers}, methods {result.methods}, "
        f"platforms {result.platforms}, countries {result.countries}, "
        f"observations {result.observations}"
    )


@app.command("eval")
def eval_cmd(
    report: Annotated[Path | None, typer.Option(help="Write JSON report here")] = None,
    min_psp_precision: Annotated[
        float | None, typer.Option(help="Gate; default from settings")
    ] = None,
    min_confidence: Annotated[str, typer.Option(help="low | medium | high")] = "low",
    clickhouse: Annotated[bool, typer.Option(help="Use ClickHouse for per-rule metrics")] = True,
) -> None:
    """Precision/recall/F1 on the gold set; exit 1 below the PSP precision gate (FR-QA-02)."""
    settings = get_settings()
    threshold = (
        settings.quality.min_psp_precision if min_psp_precision is None else min_psp_precision
    )
    ch = None
    if clickhouse:
        try:
            ch = make_ch_client(settings.clickhouse)
            ch.command("SELECT 1")
        except Exception as exc:
            log.warning("clickhouse unavailable, per-rule metrics skipped", error=str(exc))
            ch = None
    with session_scope(get_engine()) as session:
        result = eval_mod.evaluate(session, ch=ch, min_confidence=ConfidenceLevel(min_confidence))
    for line in eval_mod.summary_lines(result):
        typer.echo(line)
    verdict = eval_mod.gate(result, min_psp_precision=threshold)
    typer.echo(f"gate: {'PASS' if verdict.passed else 'FAIL'} - {verdict.reason}")
    if report is not None:
        payload = result.as_dict()
        payload["gate"] = {
            "passed": verdict.passed,
            "reason": verdict.reason,
            "threshold": threshold,
        }
        report.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        typer.echo(f"report written to {report}")
    if not verdict.passed:
        sys.exit(1)


@flags_app.command("list")
def flags_list() -> None:
    with session_scope(get_engine()) as session:
        values = FlagService(session, get_settings().flags).all()
    for key, enabled in values.items():
        typer.echo(f"{key} = {'on' if enabled else 'off'}")


@flags_app.command("set")
def flags_set(
    key: str,
    value: Annotated[str, typer.Argument(help="on | off")],
    actor: Annotated[str, typer.Option(help="Who changes the flag (audited)")] = "cli",
) -> None:
    enabled = value.lower() in {"on", "true", "1", "yes"}
    with session_scope(get_engine()) as session:
        FlagService(session, get_settings().flags).set(key, enabled, actor=actor)
    typer.echo(f"{key} = {'on' if enabled else 'off'} (audited as {actor})")


@audit_app.command("verify")
def audit_verify() -> None:
    """Recompute the audit hash chain; exit 1 if a row was altered (NFR-S-11)."""
    with session_scope(get_engine()) as session:
        result = audit_mod.verify_chain(session)
    if result.ok:
        typer.echo(f"audit chain ok ({result.rows} rows)")
        return
    typer.echo(
        f"audit chain BROKEN at row id {result.first_broken_id} ({result.rows} rows checked)"
    )
    sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    app()
