"""FR-DS-06/07: DNS updates hosts, marks no_dns, applies NS parking, rechecks after 30 days."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.core.clock import FixedClock
from payintel.core.models.base import DomainSourceKind, DomainStatus
from payintel.core.models.domains import Domain, Host
from payintel.discovery.dns import DnsRecord, StaticResolver, hosts_due, resolve_hosts
from payintel.discovery.ingest import ingest
from payintel.discovery.sources import SourceRecord

pytestmark = pytest.mark.integration


def _seed(session: Session, clock: FixedClock) -> None:
    ingest(
        session,
        [
            SourceRecord("alive.de"),
            SourceRecord("shop.alive.de"),
            SourceRecord("dead.de"),
            SourceRecord("parked.de"),
            SourceRecord("flaky.de"),
        ],
        source=DomainSourceKind.MANUAL,
        origin="t",
        clock=clock,
    )


def test_resolve_marks_statuses(db_session: Session, fixed_clock: FixedClock) -> None:
    _seed(db_session, fixed_clock)
    resolver = StaticResolver(
        {
            "alive.de": DnsRecord(
                "alive.de", a=("192.0.2.10",), ns=("ns1.alive.de",), mx=("mail.alive.de",)
            ),
            "shop.alive.de": DnsRecord("shop.alive.de", cname="alive.de", a=("192.0.2.10",)),
            "parked.de": DnsRecord("parked.de", a=("192.0.2.20",), ns=("ns1.sedoparking.com",)),
            "flaky.de": DnsRecord("flaky.de", error="Timeout"),
        }
    )
    due = hosts_due(db_session, clock=fixed_clock, recheck_days=30, limit=100)
    assert len(due) == 5
    result = asyncio.run(resolve_hosts(db_session, due, resolver, clock=fixed_clock))
    assert (result.checked, result.ok, result.no_dns, result.errors, result.parked) == (
        5,
        3,
        1,
        1,
        1,
    )
    status = {
        d.etld1: (d.status, d.status_reason) for d in db_session.execute(select(Domain)).scalars()
    }
    assert status["dead.de"] == (DomainStatus.NO_DNS, "no_a_aaaa")
    assert status["parked.de"] == (DomainStatus.PARKED, "sedo_ns")
    assert status["alive.de"][0] == DomainStatus.CANDIDATE
    assert status["flaky.de"][0] == DomainStatus.CANDIDATE  # errors do not change status
    shop = db_session.execute(select(Host).where(Host.hostname == "shop.alive.de")).scalar_one()
    assert (
        shop.cname == "alive.de"
        and shop.last_resolved_ips == ["192.0.2.10"]
        and shop.dns_status == "ok"
    )
    alive = db_session.execute(select(Host).where(Host.hostname == "alive.de")).scalar_one()
    assert alive.mx == ["mail.alive.de"] and alive.ns == ["ns1.alive.de"]
    # nothing is due until the recheck window passes (FR-DS-06: 30 days)
    assert hosts_due(db_session, clock=fixed_clock, recheck_days=30, limit=100) == []
    fixed_clock.advance(days=31)
    assert len(hosts_due(db_session, clock=fixed_clock, recheck_days=30, limit=100)) == 5
    # recovery: dead.de now resolves → back to candidate
    resolver.records["dead.de"] = DnsRecord("dead.de", a=("192.0.2.30",))
    dead = db_session.execute(select(Host).where(Host.hostname == "dead.de")).scalar_one()
    asyncio.run(resolve_hosts(db_session, [dead], resolver, clock=fixed_clock))
    d = db_session.execute(select(Domain).where(Domain.etld1 == "dead.de")).scalar_one()
    assert d.status == DomainStatus.CANDIDATE and d.status_reason == "dns_recovered"
