"""`payintel` command-line interface (Makefile targets wrap these commands)."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Annotated

import typer
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy.orm import sessionmaker

from payintel.core import audit as audit_mod
from payintel.core.ch import apply_migrations, make_ch_client
from payintel.core.clock import SYSTEM_CLOCK
from payintel.core.db import get_engine, session_scope
from payintel.core.flags import FlagService
from payintel.core.logging import configure_logging, get_logger
from payintel.core.models.base import ConfidenceLevel, DomainSourceKind, ScanType
from payintel.core.reference_loader import load_reference, sync_reference
from payintel.core.s3 import ObjectStore, make_s3_client
from payintel.core.settings import get_settings
from payintel.crawl.light.runtime import build_context
from payintel.crawl.light.worker import LightScanner, run_batch, run_pipeline
from payintel.detect.rules import load_rules, sync_rules
from payintel.discovery import dns as dns_mod
from payintel.discovery.ingest import ingest
from payintel.discovery.psl import PSL_PATH, SuffixList
from payintel.discovery.sources import parse_source
from payintel.quality import eval as eval_mod
from payintel.quality.findings import import_findings_csv
from payintel.quality.gold import gold_size, import_gold_csv
from payintel.scheduler import planner
from payintel.scheduler.politeness import RedisRateLimiter

app = typer.Typer(no_args_is_help=True, add_completion=False, help="PayIntel operations CLI")
gold_app = typer.Typer(no_args_is_help=True, help="Gold set (FR-QA-01)")
flags_app = typer.Typer(no_args_is_help=True, help="Feature flags (FR-ADM-05)")
audit_app = typer.Typer(no_args_is_help=True, help="Audit log (FR-AB-01, NFR-S-11)")
discovery_app = typer.Typer(no_args_is_help=True, help="Domain discovery (FR-DS-*)")
scheduler_app = typer.Typer(no_args_is_help=True, help="Scan planning (FR-SC-*)")
quality_app = typer.Typer(no_args_is_help=True, help="Quality dashboard and stop review (FR-QA-*)")
app.add_typer(gold_app, name="gold")
app.add_typer(flags_app, name="flags")
app.add_typer(audit_app, name="audit")
app.add_typer(discovery_app, name="discovery")
app.add_typer(scheduler_app, name="scheduler")
app.add_typer(quality_app, name="quality")

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


# --- stage 1: discovery -------------------------------------------------------


@discovery_app.command("import")
def discovery_import(
    path: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    source: Annotated[
        str, typer.Option(help="tranco | commoncrawl | ct | manual | czds")
    ] = "manual",
    origin: Annotated[str | None, typer.Option(help="Lineage label; default file name")] = None,
) -> None:
    """Ingest a source file into domain/host/domain_source with lineage (FR-DS-01..05)."""
    kind = DomainSourceKind(source)
    with session_scope(get_engine()) as session:
        r = ingest(session, parse_source(kind, path), source=kind, origin=origin or path.name)
    typer.echo(
        f"{kind.value}: batch {r.batch_id}; records {r.records_seen} "
        f"(invalid {r.records_invalid}); domains +{r.domains_new} ~{r.domains_updated}; "
        f"hosts +{r.hosts_new}"
    )
    for sample in r.invalid_samples:
        typer.echo(f"  invalid: {sample}")


@discovery_app.command("resolve")
def discovery_resolve(
    limit: Annotated[int, typer.Option(help="Hosts per run")] = 10_000,
    recheck_days: Annotated[
        int | None,
        typer.Option(help="Re-check interval; default settings.scan.no_dns_recheck_days"),
    ] = None,
) -> None:
    """Resolve A/AAAA/CNAME/MX/NS through the local Unbound; mark no_dns and parked (FR-DS-06)."""
    settings = get_settings()
    resolver = dns_mod.UnboundResolver(
        settings.discovery.dns_nameservers,
        settings.discovery.dns_port,
        settings.discovery.dns_timeout_seconds,
    )
    days = settings.scan.no_dns_recheck_days if recheck_days is None else recheck_days
    with session_scope(get_engine()) as session:
        hosts = dns_mod.hosts_due(session, clock=SYSTEM_CLOCK, recheck_days=days, limit=limit)
        r = asyncio.run(
            dns_mod.resolve_hosts(
                session, hosts, resolver, concurrency=settings.discovery.dns_concurrency
            )
        )
    typer.echo(
        f"dns: checked {r.checked}, ok {r.ok}, no_dns {r.no_dns}, errors {r.errors}, "
        f"parked {r.parked}"
    )


@discovery_app.command("update-psl")
def discovery_update_psl(
    path: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    """Replace the repository PSL snapshot after validating the downloaded file (FR-DS-04)."""
    candidate = SuffixList(path)
    shutil.copyfile(path, PSL_PATH)
    typer.echo(f"psl: {candidate.rule_count} rules written to {PSL_PATH} (commit the change)")


# --- stage 1: scheduler -------------------------------------------------------


@scheduler_app.command("plan")
def scheduler_plan(
    scan_type: Annotated[str, typer.Option(help="light | checkout")] = "light",
    limit: Annotated[int, typer.Option(help="Max new plans per run")] = 10_000,
) -> None:
    """Create/refresh scan_plan rows with priorities; drop plans of opted-out domains."""
    settings = get_settings()
    with session_scope(get_engine()) as session:
        r = planner.ensure_plans(session, ScanType(scan_type), s=settings.scan, limit=limit)
    typer.echo(
        f"{scan_type}: plans +{r.created} ~{r.updated}, removed (opt-out) {r.removed_optout}"
    )


@scheduler_app.command("prioritize")
def scheduler_prioritize(
    domains: Annotated[list[str], typer.Argument(help="eTLD+1 names")],
    scan_type: Annotated[str, typer.Option(help="light | checkout")] = "light",
    actor: Annotated[str, typer.Option(help="Audited actor")] = "cli",
) -> None:
    """Move the domains to the front of the queue (FR-SC-07, audited)."""
    settings = get_settings()
    with session_scope(get_engine()) as session:
        n = planner.prioritize_manual(
            session, domains, scan_type=ScanType(scan_type), actor=actor, s=settings.scan
        )
    typer.echo(f"{scan_type}: {n} plans prioritised (audited as {actor})")


# --- stage 1: light worker ----------------------------------------------------


@app.command("worker-light")
def worker_light(
    once: Annotated[bool, typer.Option(help="Process one batch and exit")] = False,
    limit: Annotated[int, typer.Option(help="Tasks leased per batch")] = 200,
    concurrency: Annotated[
        int | None,
        typer.Option(help="Parallel scans; default settings.light.concurrency_per_worker"),
    ] = None,
    worker_id: Annotated[str, typer.Option(help="Lease owner id")] = "worker-light-0",
    poll_seconds: Annotated[float, typer.Option(help="Sleep when the queue is empty")] = 5.0,
    allow_private: Annotated[
        bool,
        typer.Option(
            help="LOCAL TEST STANDS ONLY: system resolver and private targets allowed (NFR-S-09)"
        ),
    ] = False,
) -> None:
    """Lease light tasks and scan them (4.3); observations go to ClickHouse, artefacts to S3."""
    from redis.asyncio import Redis

    settings = get_settings()
    store = ObjectStore(make_s3_client(settings.s3))
    store.ensure_bucket(settings.s3.bucket_artifacts)
    ch = make_ch_client(settings.clickhouse)
    redis = Redis.from_url(settings.redis.url)
    ctx = build_context(
        settings,
        worker_id=worker_id,
        ch_client=ch,
        store=store,
        limiter=RedisRateLimiter(redis),
        allow_private=allow_private,
    )
    scanner = LightScanner(ctx)
    factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    conc = concurrency or settings.light.concurrency_per_worker

    async def loop() -> None:
        try:
            if once:
                started = time.monotonic()
                outcomes = await run_batch(factory, scanner, limit=limit, concurrency=conc)
                elapsed = time.monotonic() - started
                typer.echo(
                    f"batch: {len(outcomes)} scans in {elapsed:.1f}s "
                    f"({len(outcomes) / elapsed if elapsed else 0:.1f}/s); "
                    f"ok {sum(o.status.value == 'ok' for o in outcomes)}"
                )
                return
            await run_pipeline(factory, scanner, concurrency=conc, poll_seconds=poll_seconds)
        finally:
            ctx.buffer.flush()
            await ctx.fetcher.aclose()
            await redis.aclose()

    asyncio.run(loop())


# --- stage 2: quality dashboard and stop review ----------------------------------


@quality_app.command("dashboard")
def quality_dashboard(
    days: Annotated[int, typer.Option(help="Window in days")] = 7,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON")] = False,
) -> None:
    """FR-QA-03: walk outcomes per platform, confidence, freshness, change events per day."""
    from payintel.quality import dashboard as dash_mod

    settings = get_settings()
    ch = make_ch_client(settings.clickhouse)
    with session_scope(get_engine()) as session:
        d = dash_mod.dashboard(
            session,
            ch,
            now=SYSTEM_CLOCK.now(),
            days=days,
            light_cycle_days=settings.scan.light_interval_days_ecommerce,
            checkout_cycle_days=settings.scan.checkout_interval_days,
        )
    if as_json:
        typer.echo(json.dumps(d.as_dict(), ensure_ascii=False, indent=2))
    else:
        for line in dash_mod.summary_lines(d):
            typer.echo(line)


@quality_app.command("stops")
def quality_stops(
    days: Annotated[int, typer.Option(help="Window in days")] = 7,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON")] = False,
) -> None:
    """FR-QA-06: stop-reason distribution by step/platform/adapter, weekly trend, releases."""
    from payintel.quality import stops as stops_mod

    ch = make_ch_client(get_settings().clickhouse)
    r = stops_mod.review(ch, now=SYSTEM_CLOCK.now(), days=days)
    if as_json:
        typer.echo(json.dumps(r.as_dict(), ensure_ascii=False, indent=2))
    else:
        for line in stops_mod.summary_lines(r):
            typer.echo(line)


@quality_app.command("stop-sample")
def quality_stop_sample(
    step: Annotated[str, typer.Argument(help="stop_step")],
    reason: Annotated[str, typer.Argument(help="stop_reason")],
    platform: Annotated[str | None, typer.Option(help="platform_id filter")] = None,
    adapter: Annotated[str | None, typer.Option(help="adapter filter")] = None,
    days: Annotated[int, typer.Option(help="Window in days")] = 7,
    csv_path: Annotated[Path | None, typer.Option("--csv", help="Write CSV here")] = None,
) -> None:
    """FR-QA-06: up to 20 domains of a cell with screenshot and DOM keys (CSV optional)."""
    from datetime import timedelta

    from payintel.quality import stops as stops_mod

    ch = make_ch_client(get_settings().clickhouse)
    now = SYSTEM_CLOCK.now()
    rows = stops_mod.sample(
        ch,
        step=step,
        reason=reason,
        since=now - timedelta(days=days),
        until=now,
        platform_id=platform,
        adapter=adapter,
    )
    if csv_path is not None:
        csv_path.write_text(stops_mod.sample_csv(rows), encoding="utf-8")
        typer.echo(f"{len(rows)} rows written to {csv_path}")
        return
    for r in rows:
        typer.echo(
            f"{r.scan_ts:%Y-%m-%d %H:%M} {r.etld1} [{r.platform_id}/{r.adapter}] "
            f"{r.detail[:80]} | {r.screenshot_key}"
        )


@quality_app.command("stop-alerts")
def quality_stop_alerts() -> None:
    """FR-QA-06: write alerts for +5 p.p. growth of a stop reason and for `other` above 5%."""
    from payintel.quality import stops as stops_mod

    settings = get_settings()
    ch = make_ch_client(settings.clickhouse)
    with session_scope(get_engine()) as session:
        created = stops_mod.detect_alerts(session, ch, now=SYSTEM_CLOCK.now(), s=settings.quality)
        for a in created:
            typer.echo(f"{a.kind} {a.subject}: {a.message}")
    typer.echo(f"{len(created)} new alert(s)")


# --- stage 2: checkout worker ---------------------------------------------------


@app.command("worker-checkout")
def worker_checkout(
    once: Annotated[bool, typer.Option(help="Process one batch and exit")] = False,
    limit: Annotated[int, typer.Option(help="Tasks leased per batch")] = 16,
    concurrency: Annotated[
        int | None,
        typer.Option(help="Parallel walks; default settings.checkout.concurrency_per_worker"),
    ] = None,
    worker_id: Annotated[str, typer.Option(help="Lease owner id")] = "worker-checkout-0",
    poll_seconds: Annotated[float, typer.Option(help="Sleep when the queue is empty")] = 5.0,
    allow_private: Annotated[
        bool,
        typer.Option(
            help="LOCAL TEST STANDS ONLY: system resolver and private targets allowed (NFR-S-09)"
        ),
    ] = False,
) -> None:
    """Lease checkout tasks and walk them in a real browser (4.4): never past the payment step."""
    from redis.asyncio import Redis

    from payintel.crawl.checkout.runtime import build_browser_pool, build_checkout_context
    from payintel.crawl.checkout.worker import CheckoutScanner
    from payintel.crawl.checkout.worker import run_batch as run_checkout_batch
    from payintel.crawl.checkout.worker import run_pipeline as run_checkout_pipeline

    settings = get_settings()
    store = ObjectStore(make_s3_client(settings.s3))
    store.ensure_bucket(settings.s3.bucket_artifacts)
    ch = make_ch_client(settings.clickhouse)
    redis = Redis.from_url(settings.redis.url)
    factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    conc = concurrency or settings.checkout.concurrency_per_worker

    async def loop() -> None:
        async with build_browser_pool(settings) as pool:
            ctx = build_checkout_context(
                settings,
                pool=pool,
                worker_id=worker_id,
                ch_client=ch,
                store=store,
                limiter=RedisRateLimiter(redis),
                allow_private=allow_private,
            )
            if ctx.accounts is None:
                log.warning(
                    "PAYINTEL_SECRETS__ENCRYPTION_KEY is not set: account registration disabled"
                )
            scanner = CheckoutScanner(ctx)
            try:
                if once:
                    started = time.monotonic()
                    outcomes = await run_checkout_batch(
                        factory, scanner, limit=limit, concurrency=conc
                    )
                    elapsed = time.monotonic() - started
                    reached = sum(o.reached_payment for o in outcomes)
                    typer.echo(
                        f"batch: {len(outcomes)} walks in {elapsed:.1f}s; "
                        f"reached payment step {reached}; "
                        f"browser restarts {pool.restarts}"
                    )
                    return
                await run_checkout_pipeline(
                    factory, scanner, concurrency=conc, poll_seconds=poll_seconds
                )
            finally:
                ctx.buffer.flush()
                await ctx.fetcher.aclose()
                await redis.aclose()

    asyncio.run(loop())


if __name__ == "__main__":  # pragma: no cover
    app()
