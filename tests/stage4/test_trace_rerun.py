"""FR-QA-06: re-run a checkout walk from the admin, with a Playwright trace on request."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.api.deps import AppState
from payintel.core.clock import FixedClock
from payintel.core.models.audit import AuditLog
from payintel.core.models.base import ScanStatus, ScanType
from payintel.core.models.domains import Domain, Host
from payintel.core.models.scans import ScanPlan, ScanRun
from payintel.scheduler import planner
from payintel.scheduler.priority import MANUAL_PRIORITY
from tests.stage3.conftest import World, login

pytestmark = pytest.mark.integration


def _host(session: Session, etld1: str) -> Host:
    return session.execute(
        select(Host).join(Domain, Domain.id == Host.domain_id).where(Domain.etld1 == etld1)
    ).scalar_one()


def _plan(session: Session, host: Host) -> ScanPlan | None:
    return session.execute(
        select(ScanPlan).where(ScanPlan.host_id == host.id, ScanPlan.scan_type == ScanType.CHECKOUT)
    ).scalar_one_or_none()


def test_admin_queues_a_traced_rerun_and_the_scan_consumes_the_flag(
    client: TestClient,
    db_session: Session,
    world: World,
    app_state: AppState,
    fixed_clock: FixedClock,
) -> None:
    host = _host(db_session, "alpha-shop.de")
    existing = _plan(db_session, host)
    if existing is not None:  # a stale lease must not delay the manual run
        existing.locked_until = fixed_clock.now()
        existing.locked_by = "w-old"
        db_session.flush()
    csrf = login(client, world.staff_analyst_email, state=app_state, session=db_session)
    page = client.get("/admin/domains?q=alpha-shop.de")
    assert page.status_code == 200 and "Re-run checkout walk" in page.text
    r = client.post(
        f"/admin/domains/{host.id}/rerun",
        data={"csrf": csrf, "trace": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "with+Playwright+trace" in r.headers["location"]
    db_session.expire_all()
    plan = _plan(db_session, host)
    assert plan is not None and plan.trace_requested and plan.priority == MANUAL_PRIORITY
    assert plan.next_scan_at == fixed_clock.now() and plan.locked_until is None
    assert plan.requested_by is not None and plan.requested_by.endswith(world.staff_analyst_email)
    row = db_session.execute(
        select(AuditLog)
        .where(AuditLog.action == "scan_plan.prioritize")
        .order_by(AuditLog.id.desc())
        .limit(1)
    ).scalar_one()
    assert row.after is not None and row.after["trace"] is True
    assert row.object_id == host.hostname
    page = client.get("/admin/domains?q=alpha-shop.de")
    assert "trace requested by" in page.text
    # a run with a stored trace is downloadable; one without is 404
    run = ScanRun(
        id=uuid.uuid4(),
        host_id=host.id,
        scan_type=ScanType.CHECKOUT,
        started_at=fixed_clock.now(),
        finished_at=fixed_clock.now(),
        status=ScanStatus.REACHED_PAYMENT_STEP,
        worker_id="w",
        ruleset_version="r",
        artifact_prefix=f"checkout/alpha-shop.de/{uuid.uuid4()}/",
        trace_key="checkout/alpha-shop.de/x/trace.zip",
    )
    db_session.add(run)
    db_session.flush()
    assert app_state.store is not None
    app_state.store.put_bytes(
        app_state.settings.s3.bucket_artifacts,
        run.trace_key,
        b"PK\x05\x06zip",
        content_type="application/zip",
    )
    r = client.get(f"/admin/domains/{host.id}/runs/{run.id}/trace")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert r.content.startswith(b"PK")
    assert "trace</a>" in client.get("/admin/domains?q=alpha-shop.de").text
    assert client.get(f"/admin/domains/{host.id}/runs/{uuid.uuid4()}/trace").status_code == 404
    # without the trace box the request is a plain prioritisation
    r = client.post(f"/admin/domains/{host.id}/rerun", data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303 and "without+trace" in r.headers["location"]
    # staff_support cannot queue runs
    client.cookies.clear()
    csrf = login(client, world.staff_support_email, state=app_state, session=db_session)
    r = client.post(
        f"/admin/domains/{host.id}/rerun", data={"csrf": csrf, "trace": "1"}, follow_redirects=False
    )
    assert r.status_code == 403


def test_opted_out_domain_is_not_queued(
    client: TestClient, db_session: Session, world: World, app_state: AppState
) -> None:
    host = _host(db_session, "optedout.de")
    csrf = login(client, world.staff_analyst_email, state=app_state, session=db_session)
    r = client.post(
        f"/admin/domains/{host.id}/rerun", data={"csrf": csrf, "trace": "1"}, follow_redirects=False
    )
    assert r.status_code == 303 and "Not+queued" in r.headers["location"]
    assert (
        client.post(
            "/admin/domains/999999/rerun", data={"csrf": csrf}, follow_redirects=False
        ).status_code
        == 404
    )


def test_light_plans_never_get_the_trace_flag(
    db_session: Session, world: World, fixed_clock: FixedClock
) -> None:
    from payintel.core.settings import Settings

    n = planner.prioritize_manual(
        db_session,
        ["alpha-shop.de"],
        scan_type=ScanType.LIGHT,
        actor="t",
        s=Settings().scan,
        clock=fixed_clock,
        trace=True,
    )
    assert n == 1
    host = _host(db_session, "alpha-shop.de")
    plan = db_session.execute(
        select(ScanPlan).where(ScanPlan.host_id == host.id, ScanPlan.scan_type == ScanType.LIGHT)
    ).scalar_one()
    assert plan.trace_requested is False and plan.requested_by == "t"
