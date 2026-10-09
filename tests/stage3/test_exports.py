"""AC-08 and FR-EX-01…06: canaries, watermark, journaled download, approval above the limit."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.api.deps import AppState
from payintel.core.clock import FixedClock
from payintel.core.models.audit import AuditLog, UsageLog
from payintel.core.models.base import ExportFormat, ExportStatus, ExportType
from payintel.core.models.exports import Canary, ExportJob
from payintel.core.s3 import ObjectStore
from payintel.entitlements.profiles import FORBIDDEN_C1_KEYS
from payintel.exports import csv as csv_mod
from payintel.exports import parquet, watermark
from payintel.exports import service as exports
from payintel.exports.worker import run_once
from tests.stage3.conftest import World, auth

pytestmark = pytest.mark.integration


def _status(session: Session, job: ExportJob) -> ExportStatus:
    session.expire(job)
    return session.execute(select(ExportJob.status).where(ExportJob.id == job.id)).scalar_one()


def _run_all(db_session: Session, app_state: AppState, export_store: ObjectStore) -> None:
    r = run_once(
        app_state.session_factory,
        settings=app_state.settings,
        clock=app_state.clock,
        store=export_store,
    )
    assert not r.failed, r.failed


def test_snapshot_export_has_canaries_watermark_and_journaled_download(
    client: TestClient,
    db_session: Session,
    world: World,
    app_state: AppState,
    export_store: ObjectStore,
    fixed_clock: FixedClock,
) -> None:
    headers = auth(world.api_key)
    r = client.post(
        "/v1/exports", json={"type": "full_snapshot", "format": "parquet"}, headers=headers
    )
    assert r.status_code == 202, r.text
    job_id = r.json()["id"]
    assert r.json()["status"] == "pending"
    _run_all(db_session, app_state, export_store)
    job = db_session.get(ExportJob, __import__("uuid").UUID(job_id))
    assert job is not None and job.status == ExportStatus.DONE and job.file_key
    assert 3 <= len(job.canary_ids) <= 10
    data = export_store.get_bytes(app_state.settings.s3.bucket_exports, job.file_key)
    meta = parquet.read_metadata(data)
    assert meta["payintel.export_id"] == job_id
    assert meta["payintel.watermark"] == str(job.watermark_id)
    assert meta["payintel.org_id"] == str(world.org.id)
    rows = parquet.read_rows(data)
    zone = app_state.settings.identity.canary_zone
    canaries = [x for x in rows if str(x["domain"]).endswith(zone)]
    real = [x for x in rows if not str(x["domain"]).endswith(zone)]
    assert len(canaries) == len(job.canary_ids)
    assert {x["domain"] for x in real} == {"alpha-shop.de", "beta-store.de", "gamma-market.de"}
    assert watermark.order_matches(rows, job.watermark_id, lambda x: str(x["domain"]))
    cols = set(rows[0].keys())
    assert not cols & FORBIDDEN_C1_KEYS
    # canary rows are registered for the organisation (FR-AB-04 groundwork)
    stored = db_session.execute(select(Canary).where(Canary.export_job_id == job.id)).scalars()
    assert {c.domain for c in stored} == {x["domain"] for x in canaries}
    # schema sidecar + dictionary
    sidecar = export_store.get_bytes(
        app_state.settings.s3.bucket_exports, job.file_key + ".schema.json"
    )
    assert b"domain" in sidecar and b"description" in sidecar
    # download: signed link, journal row, audit row, counter
    r = client.get(f"/v1/exports/{job_id}", headers=headers)
    assert r.status_code == 200 and r.json()["download_url"].startswith("http")
    assert r.json()["expires_at"]
    rows_u = list(
        db_session.execute(
            select(UsageLog).where(UsageLog.endpoint == "exports.download")
        ).scalars()
    )
    assert len(rows_u) == 1 and rows_u[0].org_id == world.org.id
    audits = list(
        db_session.execute(select(AuditLog).where(AuditLog.action == "export.download")).scalars()
    )
    assert len(audits) == 1 and audits[0].object_id == job_id
    db_session.refresh(job)
    assert job.download_count == 1
    # link expiry (72 h)
    fixed_clock.advance(seconds=73 * 3600)
    r = client.get(f"/v1/exports/{job_id}", headers=headers)
    assert r.status_code == 409


def test_csv_export_and_increment(
    client: TestClient,
    db_session: Session,
    world: World,
    app_state: AppState,
    export_store: ObjectStore,
) -> None:
    headers = auth(world.api_key)
    assert (
        client.post("/v1/exports", json={"type": "increment"}, headers=headers).status_code == 400
    )
    r = client.post(
        "/v1/exports",
        json={"type": "increment", "format": "csv", "since": "2026-09-01"},
        headers=headers,
    )
    assert r.status_code == 202, r.text
    _run_all(db_session, app_state, export_store)
    job = db_session.get(ExportJob, __import__("uuid").UUID(r.json()["id"]))
    assert job is not None and job.status == ExportStatus.DONE and job.file_key
    data = export_store.get_bytes(app_state.settings.s3.bucket_exports, job.file_key)
    meta, rows = csv_mod.read_csv(data)
    assert meta["payintel.export_id"] == str(job.id)
    zone = app_state.settings.identity.canary_zone
    assert sum(1 for x in rows if x["domain"].endswith(zone)) == len(job.canary_ids)
    assert {x["domain"] for x in rows if not x["domain"].endswith(zone)} == {
        "alpha-shop.de",
        "beta-store.de",
        "gamma-market.de",
    }
    assert not set(rows[0]) & FORBIDDEN_C1_KEYS


def test_aggregates_export_and_segment_restriction(
    client: TestClient,
    db_session: Session,
    world: World,
    app_state: AppState,
    export_store: ObjectStore,
) -> None:
    headers = auth(world.api_key)
    r = client.post(
        "/v1/exports", json={"type": "market_aggregates", "countries": ["FR"]}, headers=headers
    )
    assert r.status_code == 403 and r.json()["reason"] == "outside_segment"
    r = client.post("/v1/exports", json={"type": "market_aggregates"}, headers=headers)
    assert r.status_code == 202
    _run_all(db_session, app_state, export_store)
    job = db_session.get(ExportJob, __import__("uuid").UUID(r.json()["id"]))
    assert job is not None and job.status == ExportStatus.DONE


def test_export_above_limit_needs_compliance_approval(
    client: TestClient,
    db_session: Session,
    world: World,
    app_state: AppState,
    export_store: ObjectStore,
) -> None:
    from payintel.core.models.orgs import Entitlement

    ent = db_session.execute(
        select(Entitlement).where(Entitlement.contract_id == world.contract.id)
    ).scalar_one()
    ent.export_max_rows = 2
    db_session.flush()
    headers = auth(world.api_key)
    r = client.post("/v1/exports", json={"type": "full_snapshot"}, headers=headers)
    assert r.status_code == 202 and r.json()["status"] == "awaiting_approval"
    job = db_session.get(ExportJob, __import__("uuid").UUID(r.json()["id"]))
    assert job is not None
    _run_all(db_session, app_state, export_store)
    db_session.refresh(job)
    assert _status(db_session, job) is ExportStatus.AWAITING_APPROVAL  # worker leaves it
    from payintel.core.models.base import Role
    from payintel.entitlements.check import EntitlementDenied
    from payintel.entitlements.model import Principal

    support = Principal(kind="staff", org_id=None, role=Role.STAFF_SUPPORT, email="s@x")
    with pytest.raises(EntitlementDenied):
        exports.approve(db_session, job, principal=support, clock=app_state.clock)
    compliance = Principal(kind="staff", org_id=None, role=Role.STAFF_COMPLIANCE, email="c@x")
    exports.approve(db_session, job, principal=compliance, clock=app_state.clock)
    assert _status(db_session, job) is ExportStatus.PENDING
    assert job.approved_by is None  # no user id for CLI
    _run_all(db_session, app_state, export_store)
    assert _status(db_session, job) is ExportStatus.DONE


def test_monthly_schedule_is_idempotent(
    db_session: Session, world: World, app_state: AppState, export_store: ObjectStore
) -> None:
    created = exports.schedule_periodic(
        db_session, settings=app_state.settings, clock=app_state.clock
    )
    assert {j.org_id for j in created} == {world.org.id, world.other_org.id}
    assert all(
        j.type == ExportType.FULL_SNAPSHOT and j.format == ExportFormat.PARQUET for j in created
    )
    again = exports.schedule_periodic(
        db_session, settings=app_state.settings, clock=app_state.clock
    )
    assert again == []
