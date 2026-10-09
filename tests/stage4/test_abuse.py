"""FR-AB-02…05: usage anomaly detectors, incidents with automatic restriction,
canary hit monitoring and the quarterly usage report."""

from __future__ import annotations

import io
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.abuse import canary, incidents
from payintel.abuse import usage_report as usage_mod
from payintel.abuse.detectors import LOOKUP_ENDPOINT, detect_org
from payintel.api.deps import AppState
from payintel.core.clock import FixedClock
from payintel.core.errors import NotFoundError, ValidationError
from payintel.core.models.abuse import CanaryHit, UsageReport
from payintel.core.models.audit import AuditLog, UsageLog
from payintel.core.models.exports import Canary
from payintel.core.models.orgs import Organization
from payintel.core.s3 import ObjectStore
from payintel.core.settings import AbuseSettings, ClickHouseSettings, S3Settings, Settings
from tests.stage3.conftest import World, auth, login

pytestmark = pytest.mark.integration

SMALL = AbuseSettings(
    records_min=100,
    outside_segment_min=10,
    enumeration_min_lookups=20,
    critical_enumeration_lookups=40,
    new_network_min_requests=5,
    field_attempts_min=3,
)


def _log(
    session: Session,
    org_id: uuid.UUID,
    ts: datetime,
    *,
    endpoint: str = "GET /v1/stores",
    records: int = 0,
    ip: str = "198.51.100.9",
    status: str = "200",
    denial: str | None = None,
    domain: str | None = None,
) -> None:
    params: dict[str, str] = {"status": status}
    if denial:
        params["denial"] = denial
    if domain:
        params["domain"] = domain
    session.add(
        UsageLog(
            ts=ts,
            org_id=org_id,
            endpoint=endpoint,
            params=params,
            records=records,
            ip=ip,
            duration_ms=3,
            request_id=uuid.uuid4().hex,
        )
    )


def _seed(session: Session, world: World, now: datetime) -> None:
    a, b = world.org.id, world.other_org.id
    # baseline: 30 records a day for both organisations from a known network
    for d in range(1, 15):
        _log(session, a, now - timedelta(days=d, hours=1), records=30)
        _log(session, b, now - timedelta(days=d, hours=1), records=1)
    t = now - timedelta(hours=2)
    # org A: a ×4 records spike, a third of the requests outside the segment,
    # field probing and a burst from a network never seen before
    for i in range(12):
        _log(session, a, t + timedelta(minutes=i), records=10, ip="203.0.113.5")
    for i in range(6):
        _log(session, a, t + timedelta(minutes=20 + i), status="403", denial="outside_segment")
    for i in range(3):
        _log(
            session,
            a,
            t + timedelta(minutes=30 + i),
            status="403",
            denial="field_profile_insufficient",
            ip="203.0.113.6",
        )
    # org B: walks the catalogue domain by domain
    for i in range(45):
        _log(
            session,
            b,
            t + timedelta(seconds=i),
            endpoint=LOOKUP_ENDPOINT,
            records=1,
            domain=f"shop-{i}.de",
        )
    session.flush()


def test_detectors_on_seeded_journal(
    db_session: Session, world: World, fixed_clock: FixedClock
) -> None:
    now = fixed_clock.now()
    assert detect_org(db_session, world.org.id, now=now, s=SMALL) == []
    _seed(db_session, world, now)
    found = {f.detector: f for f in detect_org(db_session, world.org.id, now=now, s=SMALL)}
    assert set(found) == {"outside_segment", "records_spike", "new_network", "field_probing"}
    assert found["records_spike"].severity == "high"
    assert found["records_spike"].details == {"records": 120, "baseline_mean": 30.0, "ratio": 4.0}
    assert found["outside_segment"].details == {"requests": 21, "outside_segment": 6}
    assert found["field_probing"].details == {"attempts": 3}
    assert found["new_network"].details == {"networks": {"203.0.0.0/16": 15}}
    other = detect_org(db_session, world.other_org.id, now=now, s=SMALL)
    assert [(f.detector, f.severity) for f in other] == [("enumeration", "critical")]
    assert other[0].details == {"lookups": 45, "distinct": 45}
    # default thresholds: only the outside_segment share (6/21 ≥ 20 %) survives
    defaults = detect_org(db_session, world.org.id, now=now, s=AbuseSettings())
    assert [f.detector for f in defaults] == ["outside_segment"]
    assert detect_org(db_session, world.other_org.id, now=now, s=AbuseSettings()) == []


def test_incidents_restriction_and_resolution(
    client: TestClient, db_session: Session, world: World, fixed_clock: FixedClock
) -> None:
    now = fixed_clock.now()
    _seed(db_session, world, now)
    assert client.get("/v1/stores", headers=auth(world.other_api_key)).status_code == 200
    r = incidents.run_all(db_session, now=now, s=SMALL, clock=fixed_clock)
    assert r.organisations == 2 and r.restricted == [world.other_org.id]
    assert len(r.incidents) == 5
    crit = next(i for i in r.incidents if i.detector == "enumeration")
    assert crit.severity == "critical" and crit.auto_restricted and crit.status == "open"
    other = db_session.get(Organization, world.other_org.id)
    assert other is not None and other.restricted_at == now
    assert other.restricted_reason is not None and other.restricted_reason.startswith("auto: enum")
    # restricted organisation: every key is refused with a machine-readable code
    resp = client.get("/v1/stores", headers=auth(world.other_api_key))
    assert resp.status_code == 403 and resp.json()["reason"] == "org_restricted"
    assert client.get("/v1/stores", headers=auth(world.api_key)).status_code == 200
    # the refusal is journalled with its denial code for the detectors
    last = db_session.execute(
        select(UsageLog)
        .where(UsageLog.org_id == world.other_org.id)
        .order_by(UsageLog.id.desc())
        .limit(1)
    ).scalar_one()
    assert last.params["denial"] == "org_restricted" and last.params["status"] == "403"
    # a second pass creates nothing new and escalates in place only
    fixed_clock.advance(seconds=600)
    r2 = incidents.run_all(db_session, now=fixed_clock.now(), s=SMALL, clock=fixed_clock)
    assert r2.incidents == [] and r2.restricted == []
    assert len(incidents.open_incidents(db_session)) == 5
    assert len(incidents.open_incidents(db_session, world.other_org.id)) == 1
    # resolving with "lift" restores access only when no open incident remains
    with pytest.raises(ValidationError):
        incidents.resolve(
            db_session,
            crit.id,
            status="maybe",
            resolution=None,
            lift_restriction=True,
            actor="staff_compliance:c",
            ip=None,
            clock=fixed_clock,
        )
    with pytest.raises(NotFoundError):
        incidents.resolve(
            db_session,
            999_999,
            status="resolved",
            resolution=None,
            lift_restriction=False,
            actor="x",
            ip=None,
            clock=fixed_clock,
        )
    done = incidents.resolve(
        db_session,
        crit.id,
        status="resolved",
        resolution="customer explained the integration test",
        lift_restriction=True,
        actor="staff_compliance:c",
        ip="10.0.0.1",
        clock=fixed_clock,
    )
    assert done.status == "resolved" and done.resolved_by == "staff_compliance:c"
    db_session.expire_all()
    other = db_session.get(Organization, world.other_org.id)
    assert other is not None and other.restricted_at is None
    assert client.get("/v1/stores", headers=auth(world.other_api_key)).status_code == 200
    with pytest.raises(ValidationError):
        incidents.resolve(
            db_session,
            crit.id,
            status="dismissed",
            resolution=None,
            lift_restriction=False,
            actor="x",
            ip=None,
            clock=fixed_clock,
        )
    actions = [
        a.action
        for a in db_session.execute(
            select(AuditLog)
            .where(AuditLog.object_id.in_([str(crit.id), str(world.other_org.id)]))
            .order_by(AuditLog.id)
        ).scalars()
    ]
    assert actions[-4:] == [
        "abuse.incident",
        "organization.restrict",
        "abuse.resolve",
        "organization.unrestrict",
    ]
    # auto-restriction can be switched off; the incident is still recorded
    _seed(db_session, world, fixed_clock.now() + timedelta(days=1))
    fixed_clock.advance(days=1)
    quiet = AbuseSettings(**{**SMALL.model_dump(), "auto_restrict": False})
    r3 = incidents.run_all(db_session, now=fixed_clock.now(), s=quiet, clock=fixed_clock)
    assert r3.restricted == []
    assert any(i.detector == "enumeration" and not i.auto_restricted for i in r3.incidents)


def test_canary_hits(db_session: Session, world: World, fixed_clock: FixedClock) -> None:
    zone = "canary.example.invalid"
    assert canary.canary_domain_of("c-abc.canary.example.invalid", zone) == f"c-abc.{zone}"
    assert canary.canary_domain_of("WWW.c-abc.canary.example.invalid.", zone) == f"c-abc.{zone}"
    assert canary.canary_domain_of("info@c-abc.canary.example.invalid", zone) == f"c-abc.{zone}"
    assert canary.canary_domain_of("canary.example.invalid", zone) is None
    assert canary.canary_domain_of("shop.example.com", zone) is None
    db_session.add(
        Canary(domain=f"c-abc.{zone}", org_id=world.org.id, created_at=fixed_clock.now())
    )
    db_session.flush()
    hits = canary.parse_lines(
        [
            "# resolver log",
            "",
            "bad-line",
            "2026-10-08T11:00:00Z www.c-abc.canary.example.invalid 203.0.113.7 A query",
            "2026-10-08T11:01:00+00:00 unrelated.example 203.0.113.8",
            "2026-10-08T11:02:00 c-abc.canary.example.invalid",
        ]
    )
    assert [h.name for h in hits] == [
        f"www.c-abc.{zone}",
        "unrelated.example",
        f"c-abc.{zone}",
    ]
    assert hits[0].source == "203.0.113.7" and hits[0].detail == "A query"
    assert hits[2].observed_at == datetime(2026, 10, 8, 11, 2, tzinfo=UTC)
    with pytest.raises(ValueError, match="kind"):
        canary.ingest(db_session, hits, kind="smoke", zone=zone, s=SMALL, clock=fixed_clock)
    r = canary.ingest(db_session, hits, kind="dns", zone=zone, s=SMALL, clock=fixed_clock)
    assert (r.lines, r.matched, r.unmatched, r.incidents) == (3, 2, 1, 1)
    row = db_session.execute(select(Canary).where(Canary.domain == f"c-abc.{zone}")).scalar_one()
    assert row.last_hit_at == datetime(2026, 10, 8, 11, 2, tzinfo=UTC)
    stored = list(db_session.execute(select(CanaryHit).order_by(CanaryHit.id)).scalars())
    assert [(h.kind, h.source) for h in stored] == [("dns", "203.0.113.7"), ("dns", None)]
    assert all(h.org_id == world.org.id for h in stored)
    inc = incidents.open_incidents(db_session, world.org.id)
    assert len(inc) == 1 and inc[0].detector == "canary_hit" and inc[0].severity == "high"
    assert inc[0].details["domain"] == f"c-abc.{zone}"
    # a second ingest adds hits but not a second open incident; no restriction (high, not critical)
    r2 = canary.ingest(db_session, hits[:1], kind="http", zone=zone, s=SMALL, clock=fixed_clock)
    assert (r2.matched, r2.incidents) == (1, 0)
    assert len(canary.recent_hits(db_session)) == 3
    org = db_session.get(Organization, world.org.id)
    assert org is not None and org.restricted_at is None


def test_quarter_arithmetic() -> None:
    q = usage_mod.Quarter.of(date(2026, 10, 8))
    assert q.period == "2026-Q4" and q.previous().period == "2026-Q3"
    assert q.start == datetime(2026, 10, 1, tzinfo=UTC)
    assert q.end == datetime(2027, 1, 1, tzinfo=UTC)
    q1 = usage_mod.Quarter(2026, 1)
    assert q1.previous() == usage_mod.Quarter(2025, 4)
    assert q1.end == datetime(2026, 4, 1, tzinfo=UTC)


def test_usage_report_xlsx_and_pages(
    client: TestClient,
    db_session: Session,
    world: World,
    app_state: AppState,
    export_store: ObjectStore,
    test_settings: Settings,
    fixed_clock: FixedClock,
) -> None:
    now = fixed_clock.now()
    _seed(db_session, world, now)
    incidents.run_all(db_session, now=now, s=SMALL, clock=fixed_clock)
    q = usage_mod.Quarter(2026, 4)
    bucket = test_settings.s3.bucket_exports
    rep = usage_mod.build(db_session, world.org, q, store=export_store, bucket=bucket, now=now)
    assert rep.period == "2026-Q4" and rep.file_key == f"usage/{world.org.id}/2026-Q4.xlsx"
    assert rep.summary["requests"] == 28 and rep.summary["records"] == 330
    assert rep.summary["errors"] == 9 and rep.summary["unique_ips"] == 3
    assert rep.summary["incidents"] == 4 and rep.summary["exports"] == 0
    wb = load_workbook(io.BytesIO(export_store.get_bytes(bucket, rep.file_key)))
    assert wb.sheetnames == ["Summary", "By endpoint", "By day", "Exports", "Incidents"]
    summary = {r[0]: r[1] for r in wb["Summary"].iter_rows(min_row=2, values_only=True)}
    assert summary["organisation"] == world.org.legal_name and summary["requests"] == 28
    endpoints = list(wb["By endpoint"].iter_rows(min_row=2, values_only=True))
    assert endpoints == [("GET /v1/stores", 28, 330)]
    days = list(wb["By day"].iter_rows(min_row=2, values_only=True))
    assert len(days) == 8 and days[-1] == ("2026-10-08", 21, 120)
    inc_rows = list(wb["Incidents"].iter_rows(min_row=2, values_only=True))
    assert len(inc_rows) == 4 and {r[2] for r in inc_rows} == {"medium", "high", "low"}
    # rebuilding the same period replaces the row instead of duplicating it
    fixed_clock.advance(seconds=60)
    rep2 = usage_mod.build(
        db_session, world.org, q, store=None, bucket=bucket, now=fixed_clock.now()
    )
    assert rep2.id == rep.id and rep2.file_key is None
    assert len(usage_mod.reports_of(db_session, world.org.id)) == 1
    rows = usage_mod.build_all(
        db_session, q.previous(), store=export_store, bucket=bucket, now=fixed_clock.now()
    )
    assert {r.org_id for r in rows} == {world.org.id, world.other_org.id}
    assert all(r.summary["requests"] == 7 for r in rows)  # the 7 baseline days of Q3
    assert len(db_session.execute(select(UsageReport)).scalars().all()) == 3

    # portal: the organisation sees its reports with a download link
    login(client, world.org_admin_email, state=app_state, session=db_session)
    page = client.get("/portal/usage")
    assert page.status_code == 200 and "2026-Q4" in page.text and "2026-Q3" in page.text
    assert "download .xlsx" in page.text
    client.cookies.clear()

    # admin: incidents page, resolve, restrict/unrestrict, build a report
    csrf = login(client, world.staff_compliance_email, state=app_state, session=db_session)
    page = client.get("/admin/abuse")
    assert page.status_code == 200 and "Open incidents (5)" in page.text
    assert "store lookups" in page.text and world.other_org.legal_name in page.text
    crit = next(i for i in incidents.open_incidents(db_session) if i.detector == "enumeration")
    r = client.post(
        f"/admin/abuse/incidents/{crit.id}/resolve",
        data={"csrf": csrf, "status": "dismissed", "resolution": "ok", "lift_restriction": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "dismissed" in r.headers["location"]
    page = client.get("/admin/abuse")
    assert "Open incidents (4)" in page.text
    db_session.expire_all()
    other = db_session.get(Organization, world.other_org.id)
    assert other is not None and other.restricted_at is None
    page = client.get(f"/admin/orgs/{world.other_org.id}")
    assert "Restrict API access" in page.text and "Lift restriction" not in page.text
    r = client.post(
        f"/admin/orgs/{world.other_org.id}/restrict",
        data={"csrf": csrf, "reason": "manual review"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    page = client.get(f"/admin/orgs/{world.other_org.id}")
    assert "Lift restriction" in page.text and "manual review" in page.text
    assert client.get("/v1/stores", headers=auth(world.other_api_key)).status_code == 403
    assert (
        client.post(
            f"/admin/orgs/{world.other_org.id}/restrict",
            data={"csrf": csrf, "reason": "  "},
            follow_redirects=False,
        ).status_code
        == 400
    )
    r = client.post(
        f"/admin/orgs/{world.other_org.id}/unrestrict",
        data={"csrf": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert client.get("/v1/stores", headers=auth(world.other_api_key)).status_code == 200
    r = client.post(
        f"/admin/orgs/{world.other_org.id}/usage-report",
        data={"csrf": csrf, "period": "2026-q4"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "2026-Q4" in r.headers["location"]
    page = client.get(f"/admin/orgs/{world.other_org.id}")
    assert "2026-Q4" in page.text and "2026-Q3" in page.text
    r = client.post(
        f"/admin/orgs/{world.other_org.id}/usage-report",
        data={"csrf": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "2026-Q3" in r.headers["location"]
    client.cookies.clear()
    login(client, world.staff_support_email, state=app_state, session=db_session)
    assert client.get("/admin/abuse").status_code == 403
    assert (
        client.post(
            f"/admin/orgs/{world.other_org.id}/restrict",
            data={"csrf": "x", "reason": "no"},
            follow_redirects=False,
        ).status_code
        == 403
    )


def test_abuse_cli(
    tmp_path: Path,
    fresh_database: str,
    ch_settings: ClickHouseSettings,
    s3_settings: S3Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typer.testing import CliRunner

    from payintel.cli import app
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
            assert runner.invoke(app, args).exit_code == 0
        result = runner.invoke(app, ["abuse", "detect", "--once"])
        assert result.exit_code == 0, (result.output, result.exception)
        assert "new incidents=0 restricted=0" in result.output
        result = runner.invoke(app, ["abuse", "incidents"])
        assert result.exit_code == 0 and "0 open incident(s)" in result.output
        log = tmp_path / "dns.log"
        log.write_text("2026-10-08T11:00:00Z c-zzz.canary.example.invalid 203.0.113.7\n")
        result = runner.invoke(app, ["abuse", "canary-hits", str(log), "--kind", "dns"])
        assert result.exit_code == 0, (result.output, result.exception)
        assert "lines=1 matched=0 unmatched=1 new incidents=0" in result.output
        result = runner.invoke(app, ["abuse", "usage-report", "--year", "2026", "--quarter", "3"])
        assert result.exit_code == 0, (result.output, result.exception)
        assert "0 report(s) for 2026-Q3" in result.output
    finally:
        get_engine().dispose()
        get_engine.cache_clear()
        get_settings.cache_clear()
