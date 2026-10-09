"""FR-SC-04/05/07, FR-DS-09, NFR-R-05: queue leasing under concurrency, expiry, idempotent runs."""

from __future__ import annotations

from datetime import timedelta

import pytest
from redis.asyncio import Redis
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session, sessionmaker

from payintel.core.clock import FixedClock
from payintel.core.models.audit import AuditLog, OptoutRequest
from payintel.core.models.base import DomainSourceKind, DomainStatus, OptoutMethod, ScanType
from payintel.core.models.domains import Domain, Host
from payintel.core.models.scans import ScanPlan
from payintel.core.settings import ScanSettings
from payintel.discovery.ingest import ingest
from payintel.discovery.sources import SourceRecord
from payintel.scheduler import planner, queue
from payintel.scheduler.politeness import RedisRateLimiter
from payintel.scheduler.priority import MANUAL_PRIORITY
from tests.aio import run_sync
from tests.conftest import FIXED_NOW

pytestmark = pytest.mark.integration
S = ScanSettings()


def _seed(session: Session, clock: FixedClock, names: list[str]) -> None:
    ingest(
        session,
        [SourceRecord(n, rank=i + 1) for i, n in enumerate(names)],
        source=DomainSourceKind.TRANCO,
        origin="q",
        clock=clock,
    )


def test_plans_skip_optout_and_excluded_tlds(db_session: Session, fixed_clock: FixedClock) -> None:
    _seed(db_session, fixed_clock, ["ok-shop.de", "opted.de", "verified-opt.de", "heavy.ru"])
    domains = {d.etld1: d for d in db_session.execute(select(Domain)).scalars()}
    domains["opted.de"].optout = True
    db_session.add(
        OptoutRequest(
            domain="verified-opt.de",
            method=OptoutMethod.DNS_TXT,
            token="t1",
            verified_at=fixed_clock.now(),
        )
    )
    for d in domains.values():
        d.status = DomainStatus.ECOMMERCE
    db_session.flush()
    r = planner.ensure_plans(db_session, ScanType.LIGHT, s=S, clock=fixed_clock)
    assert (r.created, r.updated) == (2, 0)
    planned = {
        h.hostname
        for h in db_session.execute(
            select(Host)
            .join(ScanPlan, ScanPlan.host_id == Host.id)
            .where(ScanPlan.scan_type == ScanType.LIGHT)
        ).scalars()
    }
    assert planned == {"ok-shop.de", "heavy.ru"}
    assert planner.is_opted_out(db_session, "verified-opt.de") and not planner.is_opted_out(
        db_session, "ok-shop.de"
    )
    # second run only refreshes priorities
    r2 = planner.ensure_plans(db_session, ScanType.LIGHT, s=S, clock=fixed_clock)
    assert (r2.created, r2.updated) == (0, 2)
    # checkout plans: .ru excluded (LR-22)
    rc = planner.ensure_plans(db_session, ScanType.CHECKOUT, s=S, clock=fixed_clock)
    assert rc.created == 1
    # a later opt-out removes existing plans (FR-DS-09)
    domains["ok-shop.de"].optout = True
    db_session.flush()
    r3 = planner.ensure_plans(db_session, ScanType.LIGHT, s=S, clock=fixed_clock)
    assert r3.removed_optout == 2  # light + checkout plan of ok-shop.de
    # priorities: better Tranco rank first
    plans = list(
        db_session.execute(select(ScanPlan).where(ScanPlan.scan_type == ScanType.LIGHT)).scalars()
    )
    assert len(plans) == 1 and plans[0].priority > 1.0


def test_manual_priority_is_audited(db_session: Session, fixed_clock: FixedClock) -> None:
    _seed(db_session, fixed_clock, ["a.de", "b.de", "opted.de"])
    db_session.execute(select(Domain).where(Domain.etld1 == "opted.de")).scalar_one().optout = True
    n = planner.prioritize_manual(
        db_session,
        ["A.de", "opted.de", "missing.de"],
        scan_type=ScanType.LIGHT,
        actor="staff_admin:1",
        s=S,
        clock=fixed_clock,
    )
    assert n == 1
    plan = db_session.execute(select(ScanPlan)).scalar_one()
    assert plan.priority == MANUAL_PRIORITY and plan.next_scan_at == fixed_clock.now()
    entry = db_session.execute(
        select(AuditLog).where(AuditLog.action == "scan_plan.prioritize")
    ).scalar_one()
    assert entry.object_id == "a.de" and entry.actor == "staff_admin:1"
    leased = queue.lease(
        db_session, ScanType.LIGHT, "w1", limit=10, lease_seconds=600, clock=fixed_clock
    )
    assert [p.id for p in leased] == [plan.id]
    queue.complete(
        db_session, plan, next_scan_at=fixed_clock.now() + timedelta(days=7), clock=fixed_clock
    )
    assert plan.priority == 0.0 and plan.locked_until is None


def test_fail_backoff_then_unreachable(db_session: Session, fixed_clock: FixedClock) -> None:
    _seed(db_session, fixed_clock, ["flaky.de"])
    planner.ensure_plans(db_session, ScanType.LIGHT, s=S, clock=fixed_clock)
    plan = db_session.execute(select(ScanPlan)).scalar_one()
    expected = [1, 6, 24]
    for hours in expected:
        [p] = queue.lease(
            db_session, ScanType.LIGHT, "w", limit=1, lease_seconds=60, clock=fixed_clock
        )
        assert p.id == plan.id
        unreachable = queue.fail(
            db_session, p, error="timeout", cycle=timedelta(days=30), s=S, clock=fixed_clock
        )
        assert not unreachable and p.next_scan_at == fixed_clock.now() + timedelta(hours=hours)
        assert (
            queue.lease(
                db_session, ScanType.LIGHT, "w", limit=1, lease_seconds=60, clock=fixed_clock
            )
            == []
        )
        fixed_clock.advance(seconds=hours * 3600)
    [p] = queue.lease(db_session, ScanType.LIGHT, "w", limit=1, lease_seconds=60, clock=fixed_clock)
    assert queue.fail(
        db_session, p, error="timeout", cycle=timedelta(days=30), s=S, clock=fixed_clock
    )
    domain = db_session.execute(select(Domain)).scalar_one()
    assert domain.status == DomainStatus.UNREACHABLE and p.fail_count == 0
    assert p.next_scan_at == fixed_clock.now() + timedelta(days=30)


def test_concurrent_workers_and_lease_expiry(
    migrated_engine: Engine, fixed_clock: FixedClock
) -> None:
    """Two sessions on separate connections: SKIP LOCKED prevents double leasing; an expired
    lease is taken over; the run id of the same lease is stable (idempotent retry)."""
    factory = sessionmaker(bind=migrated_engine, expire_on_commit=False)
    setup = factory()
    names = [f"conc-{i}.de" for i in range(6)]
    try:
        _seed(setup, fixed_clock, names)
        planner.ensure_plans(setup, ScanType.LIGHT, s=S, clock=fixed_clock)
        setup.commit()
        s1, s2 = factory(), factory()
        try:
            a = queue.lease(s1, ScanType.LIGHT, "w1", limit=3, lease_seconds=600, clock=fixed_clock)
            b = queue.lease(
                s2, ScanType.LIGHT, "w2", limit=10, lease_seconds=600, clock=fixed_clock
            )
            assert len(a) == 3 and len(b) == 3  # s1's rows are locked by an open transaction
            assert {p.id for p in a}.isdisjoint({p.id for p in b})
            s1.commit()
            s2.commit()
            assert (
                queue.lease(
                    s1, ScanType.LIGHT, "w1", limit=10, lease_seconds=600, clock=fixed_clock
                )
                == []
            )
            s1.commit()
            run_ids = {p.id: queue.run_id_for(p) for p in a}
            # the "crashed" worker w1 never completes; after the lease expires w3 takes over
            fixed_clock.advance(seconds=601)
            c = queue.lease(
                s2, ScanType.LIGHT, "w3", limit=10, lease_seconds=600, clock=fixed_clock
            )
            s2.commit()
            assert len(c) == 6
            for p in c:
                if p.id in run_ids:
                    assert queue.run_id_for(p) != run_ids[p.id]  # a new lease → a new run
            # same plan + same lease → same run id (retry within the lease is idempotent)
            p0 = c[0]
            assert queue.run_id_for(p0) == queue.run_id_for(p0)
            queue.heartbeat(s2, p0, lease_seconds=600, clock=fixed_clock)
            assert p0.locked_until == fixed_clock.now() + timedelta(seconds=600)
            for p in c:
                queue.release(s2, p, clock=fixed_clock)
            s2.commit()
        finally:
            s1.close()
            s2.close()
    finally:
        cleanup = factory()
        ids = select(Domain.id).where(Domain.etld1.in_(names))
        cleanup.execute(
            delete(ScanPlan).where(
                ScanPlan.host_id.in_(select(Host.id).where(Host.domain_id.in_(ids)))
            )
        )
        cleanup.execute(delete(Domain).where(Domain.etld1.in_(names)))
        cleanup.commit()
        cleanup.close()
        setup.close()
        fixed_clock.set(FIXED_NOW)


def test_redis_rate_limiter(redis_url: str) -> None:
    async def run() -> tuple[list[float], bool, bool, bool]:
        r = Redis.from_url(redis_url)
        lim = RedisRateLimiter(r, prefix="payintel:test:")
        await r.delete("payintel:test:host:x.de", "payintel:test:slot:etld1:x.de")
        waits = [await lim.acquire("host:x.de", rate=1.0) for _ in range(3)]
        a = await lim.take_slot("etld1:x.de", 1, 30)
        b = await lim.take_slot("etld1:x.de", 1, 30)
        await lim.release_slot("etld1:x.de")
        c = await lim.take_slot("etld1:x.de", 1, 30)
        await r.aclose()
        return waits, a, b, c

    waits, a, b, c = run_sync(run())
    assert waits[0] == 0.0 and 0.9 < waits[1] <= 1.0 and 1.9 < waits[2] <= 2.0
    assert (a, b, c) == (True, False, True)
