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
from sqlalchemy.orm import Session, sessionmaker

from payintel.api.app import create_app
from payintel.core import audit as audit_mod
from payintel.core.ch import apply_migrations, make_ch_client
from payintel.core.clock import SYSTEM_CLOCK
from payintel.core.db import get_engine, session_scope
from payintel.core.flags import FlagService
from payintel.core.logging import configure_logging, get_logger
from payintel.core.models.base import ConfidenceLevel, DomainSourceKind, Role, ScanType
from payintel.core.reference_loader import load_reference, sync_reference
from payintel.core.s3 import ObjectStore, make_s3_client
from payintel.core.settings import get_settings
from payintel.crawl.light.runtime import build_context
from payintel.crawl.light.worker import LightScanner, run_batch, run_pipeline
from payintel.detect import admin as rule_admin
from payintel.detect.rules import RuleSet, load_rules, sync_rules
from payintel.discovery import dns as dns_mod
from payintel.discovery.ingest import ingest
from payintel.discovery.psl import PSL_PATH, SuffixList
from payintel.discovery.sources import parse_source
from payintel.entitlements.model import Principal
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
api_app = typer.Typer(no_args_is_help=True, help="HTTP API, portal and admin (FR-API-*, FR-UI-*)")
alerts_app = typer.Typer(no_args_is_help=True, help="Alert matching and delivery (FR-AL-*)")
exports_app = typer.Typer(no_args_is_help=True, help="Export jobs (FR-EX-*)")
report_app = typer.Typer(no_args_is_help=True, help="C Report builder (FR-RP-*)")
optout_app = typer.Typer(no_args_is_help=True, help="Opt-out requests (FR-OO-02)")
dsar_app = typer.Typer(no_args_is_help=True, help="GDPR data subject requests (FR-OO-03)")
users_app = typer.Typer(no_args_is_help=True, help="Portal and staff users")
keys_app = typer.Typer(no_args_is_help=True, help="API keys (FR-API-02)")
orgs_app = typer.Typer(no_args_is_help=True, help="Organisations (FR-KYC-01)")
abuse_app = typer.Typer(no_args_is_help=True, help="Anti-abuse detectors and canaries (FR-AB-*)")
app.add_typer(api_app, name="api")
app.add_typer(alerts_app, name="alerts")
app.add_typer(exports_app, name="exports")
app.add_typer(abuse_app, name="abuse")
app.add_typer(report_app, name="report")
app.add_typer(optout_app, name="optout")
app.add_typer(dsar_app, name="dsar")
app.add_typer(users_app, name="users")
app.add_typer(keys_app, name="keys")
app.add_typer(orgs_app, name="orgs")
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


def _effective_ruleset(factory: sessionmaker[Session]) -> RuleSet:
    """YAML rules with admin changes from `detection_rule` applied (FR-ADM-02)."""
    base = load_rules(reference=load_reference())
    with factory() as session:
        return rule_admin.overlay(session, base)


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


@app.command("redetect")
def redetect(
    limit: Annotated[int, typer.Option(help="Hosts per run (newest artefacts first)")] = 100,
    since_days: Annotated[
        int | None, typer.Option(help="Only hosts with artefacts of the last N days")
    ] = None,
    host: Annotated[list[str] | None, typer.Option(help="Hostname(s) to re-detect")] = None,
    apply: Annotated[bool, typer.Option("--apply", help="Write results; default dry run")] = False,
    csv_path: Annotated[
        Path | None, typer.Option("--csv", help="Dry run: write findings CSV (quality eval)")
    ] = None,
) -> None:
    """FR-DT-12: re-run detection on stored artefacts with the current rules (no new scan)."""
    from payintel.crawl.light.runtime import provider_roles
    from payintel.detect import redetect as rd
    from payintel.detect.admin import overlay

    settings = get_settings()
    reference = load_reference()
    store = ObjectStore(make_s3_client(settings.s3))
    reader = rd.ArtifactReader(store, settings.s3.bucket_artifacts)
    results: list[rd.RedetectResult] = []
    now = SYSTEM_CLOCK.now()
    with session_scope(get_engine()) as session:
        ruleset = overlay(session, load_rules(reference=reference))
        if host:
            from sqlalchemy import select as sa_select

            from payintel.core.models.domains import Host

            ids = [
                int(h)
                for h in session.execute(
                    sa_select(Host.id).where(Host.hostname.in_([x.lower() for x in host]))
                ).scalars()
            ]
        else:
            ids = rd.candidate_hosts(session, limit=limit, since_days=since_days, now=now)
        for host_id in ids:
            r = rd.redetect_host(
                session,
                reader,
                ruleset,
                host_id,
                provider_roles=provider_roles(reference),
                apply=apply,
                actor="cli:redetect",
                clock=SYSTEM_CLOCK,
            )
            results.append(r)
            typer.echo(rd.summary_line(r))
    if csv_path is not None:
        csv_path.write_text(rd.to_csv(results), encoding="utf-8")
        typer.echo(f"findings written to {csv_path}")
    changed = sum(1 for r in results if r.changes)
    typer.echo(
        f"ruleset {ruleset.version}: {len(results)} host(s), {changed} with changes"
        f"{' (applied)' if apply else ' (dry run)'}"
    )


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
    factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    ctx = build_context(
        settings,
        worker_id=worker_id,
        ch_client=ch,
        store=store,
        limiter=RedisRateLimiter(redis),
        allow_private=allow_private,
        ruleset=_effective_ruleset(factory),
    )
    scanner = LightScanner(ctx)
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


@quality_app.command("anomalies")
def quality_anomalies() -> None:
    """FR-QA-04: alert on provider_removed spikes (>3x the 7-day mean per provider)."""
    from payintel.quality import anomalies as an

    settings = get_settings()
    with session_scope(get_engine()) as session:
        created = an.detect(
            session,
            now=SYSTEM_CLOCK.now(),
            multiplier=settings.quality.anomaly_removed_multiplier,
        )
        for a in created:
            typer.echo(f"{a.kind} {a.subject}: {a.message}")
        open_now = an.open_spike_alerts(session)
    typer.echo(f"{len(created)} new alert(s), {len(open_now)} provider(s) on hold")


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
    ruleset = _effective_ruleset(factory)

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
                ruleset=ruleset,
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


# --- stage 3: API, portal, alerts, exports, reports, compliance ------------------


def _cli_principal(actor: str) -> Principal:
    """Staff-admin principal for operator commands (audited as `staff_admin:cli:<actor>`)."""
    return Principal(kind="staff", org_id=None, role=Role.STAFF_ADMIN, email=f"cli:{actor}")


@api_app.command("serve")
def api_serve(
    host: Annotated[str, typer.Option(help="Bind address")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port")] = 8000,
    workers: Annotated[int, typer.Option(help="Uvicorn workers")] = 1,
) -> None:
    """Run the API, portal and admin behind Caddy (docker-compose `api`)."""
    import uvicorn

    from payintel.api.runtime import build_state

    state = build_state(get_settings())
    application = create_app(state)
    uvicorn.run(application, host=host, port=port, workers=workers, proxy_headers=True)


@alerts_app.command("dispatch")
def alerts_dispatch(
    once: Annotated[bool, typer.Option("--once", help="One cycle, then exit")] = False,
    interval: Annotated[float, typer.Option(help="Seconds between cycles")] = 30.0,
    force_digests: Annotated[bool, typer.Option(help="Send digests now (manual run)")] = False,
) -> None:
    """Match new change events to alert rules and deliver (webhook/Telegram), with retries."""
    from payintel.alerts.worker import build_senders, run_cycle

    settings = get_settings()
    factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    webhook, telegram = build_senders(settings, clock=SYSTEM_CLOCK)
    while True:
        r = run_cycle(
            factory,
            settings=settings,
            clock=SYSTEM_CLOCK,
            webhook_sender=webhook,
            telegram_sender=telegram,
            force_digests=force_digests,
        )
        typer.echo(
            f"events={r.matched.events_seen} new deliveries={r.matched.deliveries_created} "
            f"held={r.matched.deliveries_held} anomalies={r.anomalies}; "
            f"attempted={r.dispatched.attempted} delivered={r.dispatched.delivered} "
            f"failed={r.dispatched.failed}"
        )
        if once:
            return
        time.sleep(interval)


@abuse_app.command("detect")
def abuse_detect(
    once: Annotated[bool, typer.Option("--once", help="One pass, then exit")] = False,
    interval: Annotated[float, typer.Option(help="Seconds between passes")] = 900.0,
) -> None:
    """FR-AB-02/03: run the usage anomaly detectors; critical findings restrict the organisation."""
    from payintel.abuse import incidents as incidents_mod

    settings = get_settings()
    while True:
        with session_scope(get_engine()) as session:
            r = incidents_mod.run_all(
                session, now=SYSTEM_CLOCK.now(), s=settings.abuse, clock=SYSTEM_CLOCK
            )
            for inc in r.incidents:
                typer.echo(f"{inc.severity} {inc.detector} org={inc.org_id}: {inc.summary}")
        typer.echo(
            f"organisations={r.organisations} new incidents={len(r.incidents)} "
            f"restricted={len(r.restricted)}"
        )
        if once:
            return
        time.sleep(interval)


@abuse_app.command("canary-hits")
def abuse_canary_hits(
    path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="Log lines")],
    kind: Annotated[str, typer.Option(help="dns | http | email")] = "dns",
) -> None:
    """FR-AB-04: ingest canary accesses (`<iso-ts> <name> [source] [detail]` per line)."""
    from payintel.abuse import canary as canary_mod

    settings = get_settings()
    hits = canary_mod.parse_lines(path.read_text(encoding="utf-8").splitlines())
    with session_scope(get_engine()) as session:
        r = canary_mod.ingest(
            session,
            hits,
            kind=kind,
            zone=settings.identity.canary_zone,
            s=settings.abuse,
            clock=SYSTEM_CLOCK,
        )
    typer.echo(
        f"lines={r.lines} matched={r.matched} unmatched={r.unmatched} new incidents={r.incidents}"
    )


@abuse_app.command("incidents")
def abuse_incidents() -> None:
    """List open incidents."""
    from payintel.abuse import incidents as incidents_mod

    with session_scope(get_engine()) as session:
        rows = incidents_mod.open_incidents(session)
        for inc in rows:
            typer.echo(
                f"{inc.id} {inc.created_at:%Y-%m-%d %H:%M} {inc.severity} {inc.detector} "
                f"org={inc.org_id} {inc.summary}"
            )
    typer.echo(f"{len(rows)} open incident(s)")


@abuse_app.command("usage-report")
def abuse_usage_report(
    year: Annotated[int | None, typer.Option(help="Default: previous quarter")] = None,
    quarter: Annotated[int | None, typer.Option(help="1-4")] = None,
    org: Annotated[list[str] | None, typer.Option(help="Organisation id(s)")] = None,
) -> None:
    """FR-AB-05: build the quarterly usage report (XLSX in the exports bucket) per organisation."""
    import uuid as _uuid

    from payintel.abuse import usage_report as usage_mod

    settings = get_settings()
    now = SYSTEM_CLOCK.now()
    q = (
        usage_mod.Quarter(year, quarter)
        if year and quarter
        else usage_mod.Quarter.of(now.date()).previous()
    )
    store = ObjectStore(make_s3_client(settings.s3))
    store.ensure_bucket(settings.s3.bucket_exports)
    with session_scope(get_engine()) as session:
        rows = usage_mod.build_all(
            session,
            q,
            store=store,
            bucket=settings.s3.bucket_exports,
            now=now,
            org_ids=[_uuid.UUID(o) for o in org] if org else None,
        )
        for r in rows:
            typer.echo(f"{r.org_id} {r.period}: {r.summary['requests']} requests → {r.file_key}")
    typer.echo(f"{len(rows)} report(s) for {q.period}")


@exports_app.command("run")
def exports_run(
    once: Annotated[bool, typer.Option("--once", help="One pass, then exit")] = False,
    interval: Annotated[float, typer.Option(help="Seconds between passes")] = 60.0,
    limit: Annotated[int, typer.Option(help="Jobs per pass")] = 10,
) -> None:
    """Schedule periodic exports and build pending jobs into S3 (FR-EX-03/04/05)."""
    from payintel.exports.worker import run_once

    settings = get_settings()
    store = ObjectStore(make_s3_client(settings.s3))
    store.ensure_bucket(settings.s3.bucket_exports)
    factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    while True:
        r = run_once(factory, settings=settings, clock=SYSTEM_CLOCK, store=store, limit=limit)
        typer.echo(f"scheduled={r.scheduled} built={r.built} failed={len(r.failed)}")
        if once:
            return
        time.sleep(interval)


@report_app.command("build")
def report_build(
    country: Annotated[list[str], typer.Option("--country", help="ISO-2, repeatable")],
    platform: Annotated[list[str] | None, typer.Option("--platform")] = None,
    vertical: Annotated[list[str] | None, typer.Option("--vertical")] = None,
    period_start: Annotated[str | None, typer.Option(help="YYYY-MM-DD")] = None,
    period_end: Annotated[str | None, typer.Option(help="YYYY-MM-DD")] = None,
    out: Annotated[Path, typer.Option(help="Directory for the XLSX and CSV zip")] = Path("."),
    actor: Annotated[str, typer.Option(help="Audit actor")] = "cli",
) -> None:
    """Build a C Report (XLSX + CSV) for one or more countries (FR-RP-01…04, AC-15)."""
    from datetime import date as _date

    from payintel.reports import aggregates
    from payintel.reports import service as report_service

    settings = get_settings()
    spec = aggregates.ReportSpec(
        countries=tuple(c.upper() for c in country),
        platforms=tuple(platform or ()),
        verticals=tuple(vertical or ()),
        period_start=_date.fromisoformat(period_start) if period_start else None,
        period_end=_date.fromisoformat(period_end) if period_end else None,
        min_cell=settings.quality.report_min_cell_size,
    )
    ch = None
    try:
        ch = make_ch_client(settings.clickhouse)
        ch.command("SELECT 1")
    except Exception as exc:
        log.warning(
            "clickhouse unavailable, monthly dynamics use the Postgres fallback", error=str(exc)
        )
        ch = None
    with session_scope(get_engine()) as session:
        job, xlsx, csv_zip = report_service.build_report(
            session,
            spec,
            principal=_cli_principal(actor),
            store=None,
            settings=settings,
            clock=SYSTEM_CLOCK,
            ch=ch,
        )
    out.mkdir(parents=True, exist_ok=True)
    (out / f"payintel-report-{job.id}.xlsx").write_bytes(xlsx)
    (out / f"payintel-report-{job.id}.csv.zip").write_bytes(csv_zip)
    typer.echo(f"report {job.id}: {json.dumps(job.summary, ensure_ascii=False)} → {out}")


@optout_app.command("verify-pending")
def optout_verify_pending() -> None:
    """Check DNS TXT / well-known proofs of pending opt-out requests and apply confirmed ones."""
    from payintel.compliance import optout, resolvers

    settings = get_settings()
    dns_txt = resolvers.dns_txt_lookup(settings)
    verified = 0
    with session_scope(get_engine()) as session:
        for req in optout.pending_requests(session):
            if optout.verify(
                session, req, dns_txt=dns_txt, http_get=resolvers.http_get, clock=SYSTEM_CLOCK
            ):
                verified += 1
                typer.echo(f"verified: {req.domain}")
        applied = optout.apply_verified(session, clock=SYSTEM_CLOCK)
    typer.echo(f"verified {verified}, applied {applied}")


@optout_app.command("apply")
def optout_apply() -> None:
    """Apply verified opt-outs: flag domains, drop scan plans (FR-OO-02, ≤72 h)."""
    from payintel.compliance import optout

    with session_scope(get_engine()) as session:
        applied = optout.apply_verified(session, clock=SYSTEM_CLOCK)
    typer.echo(f"applied {applied}")


@dsar_app.command("list")
def dsar_list() -> None:
    """Open GDPR requests with deadlines; overdue ones are marked (FR-OO-03)."""
    from payintel.compliance import dsar

    with session_scope(get_engine()) as session:
        overdue = {r.id for r in dsar.overdue(session, clock=SYSTEM_CLOCK)}
        for r in dsar.open_requests(session):
            flag = " OVERDUE" if r.id in overdue else ""
            typer.echo(
                f"#{r.id} {r.kind.value} {r.status.value} due {r.due_at:%Y-%m-%d} "
                f"{r.subject} <{r.contact}>{flag}"
            )


@orgs_app.command("create")
def orgs_create(
    legal_name: Annotated[str, typer.Option(help="Legal name")],
    country: Annotated[str, typer.Option(help="ISO-2")],
    reg_number: Annotated[str | None, typer.Option(help="Registration number")] = None,
    actor: Annotated[str, typer.Option(help="Audit actor")] = "cli",
) -> None:
    """Create an applicant organisation (status `applied`)."""
    from payintel.core.models.orgs import Organization

    with session_scope(get_engine()) as session:
        org = Organization(
            legal_name=legal_name,
            reg_number=reg_number,
            country=country.upper(),
            created_at=SYSTEM_CLOCK.now(),
        )
        session.add(org)
        session.flush()
        audit_mod.record(
            session,
            actor=_cli_principal(actor).actor,
            action="org.create",
            object_type="organization",
            object_id=str(org.id),
            after={"legal_name": legal_name, "country": country.upper()},
        )
        typer.echo(str(org.id))


@orgs_app.command("list")
def orgs_list() -> None:
    from sqlalchemy import select

    from payintel.core.models.orgs import Organization

    with session_scope(get_engine()) as session:
        for o in session.execute(select(Organization).order_by(Organization.created_at)).scalars():
            typer.echo(f"{o.id} {o.status.value:16} {o.country} {o.legal_name}")


@users_app.command("create")
def users_create(
    email: Annotated[str, typer.Option(help="E-mail")],
    password: Annotated[
        str, typer.Option(help="Initial password (≥ 12 chars)", prompt=True, hide_input=True)
    ],
    role: Annotated[str, typer.Option(help="org_viewer|org_analyst|org_admin|staff_*")],
    org: Annotated[str | None, typer.Option(help="Organisation id (org roles only)")] = None,
    actor: Annotated[str, typer.Option(help="Audit actor")] = "cli",
) -> None:
    """Create a portal or staff user; 2FA enrolment happens at first login (FR-UI-01)."""
    import uuid as _uuid

    from payintel.compliance import users

    with session_scope(get_engine()) as session:
        user = users.create_user(
            session,
            email=email,
            password=password,
            role=Role(role),
            org_id=_uuid.UUID(org) if org else None,
            actor=_cli_principal(actor).actor,
            clock=SYSTEM_CLOCK,
        )
        typer.echo(f"{user.id} {user.email} {role}")


@keys_app.command("issue")
def keys_issue(
    org: Annotated[str, typer.Option(help="Organisation id")],
    name: Annotated[str, typer.Option(help="Key name")],
    scopes: Annotated[
        str, typer.Option(help="Comma-separated scopes")
    ] = "stores:read,changes:read,stats:read,usage:read",
    expires_in_days: Annotated[int | None, typer.Option()] = None,
    allowed_ips: Annotated[str | None, typer.Option(help="Comma-separated IPs/CIDRs")] = None,
    actor: Annotated[str, typer.Option(help="Audit actor")] = "cli",
) -> None:
    """Issue an API key; the key is printed once and never stored (FR-API-02)."""
    import uuid as _uuid

    from payintel.api.auth import keys as keys_mod

    settings = get_settings()
    pepper = settings.secrets.api_key_pepper.get_secret_value()
    if not pepper:
        typer.echo("PAYINTEL_SECRETS__API_KEY_PEPPER is not set", err=True)
        sys.exit(2)
    with session_scope(get_engine()) as session:
        issued = keys_mod.issue_key(
            session,
            org_id=_uuid.UUID(org),
            name=name,
            scopes=[s.strip() for s in scopes.split(",") if s.strip()],
            expires_in_days=expires_in_days,
            allowed_ips=[s.strip() for s in (allowed_ips or "").split(",") if s.strip()],
            created_by=None,
            actor=_cli_principal(actor).actor,
            pepper=pepper,
            prefix=settings.api.api_key_prefix,
            clock=SYSTEM_CLOCK,
        )
        typer.echo(issued.raw)


if __name__ == "__main__":  # pragma: no cover
    app()
