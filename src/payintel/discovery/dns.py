"""DNS resolution through the project's own recursive resolver (FR-DS-06).

`Resolver` is a protocol so tests inject a `StaticResolver`; production uses
`UnboundResolver` (dnspython, async) pointed at the Unbound container. Hosts
without A/AAAA are marked `no_dns` and re-checked after
`ScanSettings.no_dns_recheck_days`; parked NS/CNAME (FR-DS-07) is applied here
because it needs no HTTP request.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

import dns.asyncresolver
import dns.exception
import dns.rdatatype
import dns.resolver
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from payintel.core.clock import SYSTEM_CLOCK, Clock
from payintel.core.models.base import DomainStatus
from payintel.core.models.domains import Domain, Host
from payintel.discovery.parking import ParkingDetector, get_parking_detector


@dataclass(frozen=True)
class DnsRecord:
    hostname: str
    a: tuple[str, ...] = ()
    aaaa: tuple[str, ...] = ()
    cname: str | None = None
    mx: tuple[str, ...] = ()
    ns: tuple[str, ...] = ()
    error: str | None = None

    @property
    def addresses(self) -> tuple[str, ...]:
        return self.a + self.aaaa

    @property
    def status(self) -> str:
        if self.error:
            return "error"
        return "ok" if self.addresses else "no_dns"


class Resolver(Protocol):
    async def resolve(self, hostname: str) -> DnsRecord: ...


class StaticResolver:
    """Deterministic resolver for tests: a mapping hostname → DnsRecord."""

    def __init__(self, records: dict[str, DnsRecord]) -> None:
        self.records = records
        self.calls: list[str] = []

    async def resolve(self, hostname: str) -> DnsRecord:
        self.calls.append(hostname)
        return self.records.get(hostname, DnsRecord(hostname=hostname))


class UnboundResolver:
    def __init__(self, nameservers: Sequence[str], port: int, timeout: float) -> None:
        self._r = dns.asyncresolver.Resolver(configure=False)
        self._r.nameservers = list(nameservers)
        self._r.port = port
        self._r.timeout = timeout
        self._r.lifetime = timeout * 2

    async def _query(self, name: str, rtype: str) -> list[str]:
        try:
            answer = await self._r.resolve(name, rtype, raise_on_no_answer=False)
        except (dns.resolver.NXDOMAIN, dns.resolver.NoNameservers):
            return []
        out: list[str] = []
        for rr in answer.rrset or []:
            if rtype == "MX":
                out.append(str(rr.exchange).rstrip(".").lower())
            elif rtype in {"CNAME", "NS"}:
                out.append(str(rr.target).rstrip(".").lower())
            else:
                out.append(rr.address)
        return sorted(out)

    async def resolve(self, hostname: str) -> DnsRecord:
        try:
            a, aaaa, cname, mx, ns = await asyncio.gather(
                self._query(hostname, "A"),
                self._query(hostname, "AAAA"),
                self._query(hostname, "CNAME"),
                self._query(hostname, "MX"),
                self._query(hostname, "NS"),
            )
        except (dns.exception.Timeout, dns.exception.DNSException) as exc:
            return DnsRecord(hostname=hostname, error=type(exc).__name__)
        return DnsRecord(
            hostname=hostname,
            a=tuple(a),
            aaaa=tuple(aaaa),
            cname=cname[0] if cname else None,
            mx=tuple(mx),
            ns=tuple(ns),
        )


@dataclass
class ResolveResult:
    checked: int = 0
    ok: int = 0
    no_dns: int = 0
    errors: int = 0
    parked: int = 0
    domains_no_dns: list[str] = field(default_factory=list)


def hosts_due(session: Session, *, clock: Clock, recheck_days: int, limit: int) -> list[Host]:
    cutoff = clock.now() - timedelta(days=recheck_days)
    stmt = (
        select(Host)
        .join(Domain, Domain.id == Host.domain_id)
        .where(
            Domain.optout.is_(False),
            or_(Host.dns_checked_at.is_(None), Host.dns_checked_at < cutoff),
        )
        .order_by(Host.dns_checked_at.nulls_first(), Host.id)
        .limit(limit)
    )
    return list(session.execute(stmt).scalars())


async def resolve_hosts(
    session: Session,
    hosts: Sequence[Host],
    resolver: Resolver,
    *,
    clock: Clock = SYSTEM_CLOCK,
    concurrency: int = 50,
    parking: ParkingDetector | None = None,
) -> ResolveResult:
    parking = parking or get_parking_detector()
    sem = asyncio.Semaphore(concurrency)

    async def one(host: Host) -> DnsRecord:
        async with sem:
            return await resolver.resolve(host.hostname)

    records = await asyncio.gather(*(one(h) for h in hosts))
    result = ResolveResult()
    now = clock.now()
    for host, rec in zip(hosts, records, strict=True):
        result.checked += 1
        host.dns_checked_at = now
        host.dns_status = rec.status
        host.last_resolved_ips = list(rec.addresses) or None
        host.cname = rec.cname
        host.mx = list(rec.mx) or None
        host.ns = list(rec.ns) or None
        if rec.status == "error":
            result.errors += 1
            continue
        if rec.status == "no_dns":
            result.no_dns += 1
        else:
            result.ok += 1
        domain = session.get(Domain, host.domain_id)
        if domain is None or not host.is_primary:
            continue
        if rec.status == "no_dns":
            _set_status(domain, DomainStatus.NO_DNS, "no_a_aaaa", now)
            result.domains_no_dns.append(domain.etld1)
            continue
        verdict = parking.check_dns(ns=host.ns, cname=host.cname)
        if verdict.parked:
            _set_status(domain, DomainStatus.PARKED, verdict.signature_id, now)
            result.parked += 1
        elif domain.status in {DomainStatus.NO_DNS, DomainStatus.PARKED}:
            _set_status(domain, DomainStatus.CANDIDATE, "dns_recovered", now)
    session.flush()
    return result


def _set_status(domain: Domain, status: DomainStatus, reason: str | None, now: datetime) -> None:
    if domain.status == DomainStatus.OPTOUT:
        return
    if domain.status != status or domain.status_reason != reason:
        domain.status = status
        domain.status_reason = reason
        domain.status_changed_at = now
