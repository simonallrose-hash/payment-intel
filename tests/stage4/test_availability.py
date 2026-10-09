"""FR-SC-05 / FR-HI-03: `store_offline` when the retry budget is exhausted,
`store_online` when a later scan finds the store again; both reach the API
changes feed and the alert rules."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.alerts import matcher, rules
from payintel.core.clock import FixedClock
from payintel.core.models.base import ChangeEventType, DomainStatus, ScanStatus, ScanType
from payintel.core.models.domains import Domain
from payintel.core.models.scans import ScanPlan, ScanRun
from payintel.core.models.store import ChangeEvent
from payintel.core.settings import Settings
from payintel.history import availability
from payintel.scheduler import queue
from tests.stage3.conftest import World, auth

pytestmark = pytest.mark.integration


def _run(db_session: Session, host_id: int, clock: FixedClock) -> ScanRun:
    run = ScanRun(
        id=uuid.uuid4(),
        host_id=host_id,
        scan_type=ScanType.LIGHT,
        started_at=clock.now(),
        finished_at=clock.now(),
        status=ScanStatus.ERROR,
        worker_id="t",
        ruleset_version="t",
    )
    db_session.add(run)
    db_session.flush()
    return run


def test_offline_after_retry_budget_then_online(
    client: TestClient, db_session: Session, world: World, fixed_clock: FixedClock
) -> None:
    host = world.hosts["alpha-shop.de"]
    domain = db_session.get(Domain, host.domain_id)
    assert domain is not None and domain.status == DomainStatus.ECOMMERCE
    s = Settings().scan
    plan = ScanPlan(
        host_id=host.id,
        scan_type=ScanType.LIGHT,
        next_scan_at=fixed_clock.now(),
        updated_at=fixed_clock.now(),
    )
    db_session.add(plan)
    db_session.flush()
    rule = rules.create_rule(
        db_session,
        org_id=world.org.id,
        watchlist_id=None,
        event_types=["store_offline", "store_online"],
        provider_id=None,
        method_id=None,
        min_confidence="low",
        channel="telegram",
        webhook_id=None,
        telegram_chat_id="-1",
        digest="immediate",
        actor="t",
        clock=fixed_clock,
    )
    fixed_clock.advance(seconds=1)
    unreachable = False
    for _ in range(s.max_failures_before_unreachable):
        assert not unreachable
        run = _run(db_session, host.id, fixed_clock)
        unreachable = queue.fail(
            db_session,
            plan,
            error="timeout",
            cycle=timedelta(days=7),
            s=s,
            clock=fixed_clock,
            scan_run_id=run.id,
        )
        fixed_clock.advance(days=4)
    db_session.expire(domain)
    fresh = db_session.get(Domain, host.domain_id)
    assert unreachable and fresh is not None and fresh.status == DomainStatus.UNREACHABLE
    events = list(
        db_session.execute(
            select(ChangeEvent)
            .where(ChangeEvent.host_id == host.id, ChangeEvent.entity_type == "store")
            .order_by(ChangeEvent.id)
        ).scalars()
    )
    assert [e.event_type for e in events] == [ChangeEventType.STORE_OFFLINE]
    assert events[0].entity_id == "alpha-shop.de" and events[0].old_value == "ecommerce"
    # online again only after an offline event; a second call is idempotent
    assert availability.mark_online(
        db_session, host, domain, scan_run_id=run.id, now=fixed_clock.now()
    )
    assert (
        availability.mark_online(
            db_session, host, domain, scan_run_id=run.id, now=fixed_clock.now()
        )
        is None
    )
    kinds = [
        e.event_type
        for e in db_session.execute(
            select(ChangeEvent)
            .where(ChangeEvent.host_id == host.id, ChangeEvent.entity_type == "store")
            .order_by(ChangeEvent.id)
        ).scalars()
    ]
    assert kinds == [ChangeEventType.STORE_OFFLINE, ChangeEventType.STORE_ONLINE]
    # visible in the client changes feed and matched by the segment rule
    r = client.get("/v1/changes?from=2026-10-01T00:00:00Z", headers=auth(world.api_key))
    assert r.status_code == 200, r.text
    types = [c["type"] for c in r.json()["items"] if c["domain"] == "alpha-shop.de"]
    assert "store_offline" in types and "store_online" in types
    m = matcher.match_new_events(db_session, settings=Settings(), clock=fixed_clock)
    assert m.deliveries_created == 2
    assert rule.id is not None
    # a store that was never online through us does not emit an online event
    beta = world.hosts["beta-store.de"]
    beta_domain = db_session.get(Domain, beta.domain_id)
    assert beta_domain is not None
    assert (
        availability.mark_online(
            db_session, beta, beta_domain, scan_run_id=run.id, now=fixed_clock.now()
        )
        is None
    )
    # opted-out domains are never flipped to unreachable
    opt = world.hosts["optedout.de"]
    opt_domain = db_session.get(Domain, opt.domain_id)
    assert opt_domain is not None
    assert (
        availability.mark_offline(
            db_session, opt, opt_domain, scan_run_id=run.id, now=fixed_clock.now()
        )
        is None
    )
    assert opt_domain.status == DomainStatus.OPTOUT
