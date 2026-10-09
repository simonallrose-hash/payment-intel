"""Hoster complaints → automatic ASN rate limits until a manual review (FR-OO-04).

A complaint names an ASN or an IP address of the complaining hoster. The limit
is applied at once: every request to a host in that ASN runs at
`factor × rate` per host/IP and the whole ASN shares an `asn_rps` bucket
(`scheduler.politeness.AsnPolicy`). Workers pick the change up within
`scan.asn_limits_refresh_seconds`. Only `staff_admin` lifts a limit, after the
review; repeated complaints on a limited ASN halve the factor again.

Automated intake: `payintel crawler complaints FILE` reads one complaint per
line (`<iso-ts> <asn|AS12345|ip> <source> [note]`) exported from the abuse
mailbox or ticket system.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.core import audit
from payintel.core.clock import Clock
from payintel.core.errors import NotFoundError, ValidationError
from payintel.core.models.scans import AsnLimit
from payintel.core.settings import Settings
from payintel.crawl.asn import AsnTable
from payintel.scheduler.politeness import AsnPolicy

MIN_FACTOR = 0.01


@dataclass(frozen=True)
class ComplaintInput:
    received_at: datetime
    target: str  # "12345", "AS12345" or an IP address
    source: str
    note: str | None = None


@dataclass(frozen=True)
class IngestResult:
    lines: int
    applied: int
    unresolved: int


def parse_target(target: str, table: AsnTable | None) -> tuple[int, str | None]:
    """`AS12345` / `12345` → (12345, None); an IP → (asn from the table, ip)."""
    t = target.strip()
    if t.upper().startswith("AS") and t[2:].isdigit():
        return int(t[2:]), None
    if t.isdigit():
        return int(t), None
    info = table.lookup(t) if table is not None else None
    if info is None:
        raise ValidationError("cannot map the address to an ASN (no table or unrouted)", ip=t)
    return info.asn, t


def parse_lines(lines: list[str]) -> list[ComplaintInput]:
    out: list[ComplaintInput] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(maxsplit=3)
        if len(parts) < 3:
            continue
        ts = datetime.fromisoformat(parts[0].replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        out.append(
            ComplaintInput(ts, parts[1], parts[2], parts[3][:1000] if len(parts) > 3 else None)
        )
    return out


def record(
    session: Session,
    *,
    target: str,
    source: str,
    note: str | None,
    actor: str,
    clock: Clock,
    table: AsnTable | None = None,
    factor: float,
    asn_rps: float,
    received_at: datetime | None = None,
) -> AsnLimit:
    """Apply (or tighten) the limit for the ASN named by `target`."""
    if not source.strip():
        raise ValidationError("a source (mailbox, ticket) is required")
    if not 0 < factor <= 1:
        raise ValidationError("factor must be in (0, 1]", factor=factor)
    asn, ip = parse_target(target, table)
    now = clock.now()
    when = received_at or now
    row = session.get(AsnLimit, asn)
    info = None
    if ip and table is not None:
        info = table.lookup(ip)
    if row is None:
        row = AsnLimit(
            asn=asn,
            factor=factor,
            asn_rps=asn_rps,
            source=source.strip()[:256],
            note=note,
            ip=ip,
            asn_name=info.name[:256] if info else None,
            complaints=1,
            created_at=now,
            created_by=actor,
            last_complaint_at=when,
        )
        session.add(row)
        action = "asn_limit.set"
    else:
        if row.lifted_at is None:
            row.factor = max(MIN_FACTOR, min(row.factor, factor) / 2)  # repeated complaint
        else:  # re-applied after a lift
            row.factor, row.lifted_at, row.lifted_by = factor, None, None
        row.asn_rps = min(row.asn_rps, asn_rps)
        row.complaints += 1
        row.source = source.strip()[:256]
        row.note = note or row.note
        row.ip = ip or row.ip
        row.last_complaint_at = max(row.last_complaint_at, when)
        action = "asn_limit.tighten"
    session.flush()
    audit.record(
        session,
        actor=actor,
        action=action,
        object_type="asn_limit",
        object_id=str(asn),
        after={
            "factor": row.factor,
            "asn_rps": row.asn_rps,
            "source": row.source,
            "ip": ip,
            "complaints": row.complaints,
        },
        clock=clock,
    )
    return row


def lift(session: Session, asn: int, *, actor: str, note: str | None, clock: Clock) -> AsnLimit:
    row = session.get(AsnLimit, asn)
    if row is None or row.lifted_at is not None:
        raise NotFoundError("no active limit for this ASN", asn=asn)
    row.lifted_at = clock.now()
    row.lifted_by = actor
    if note:
        row.note = note
    session.flush()
    audit.record(
        session,
        actor=actor,
        action="asn_limit.lift",
        object_type="asn_limit",
        object_id=str(asn),
        after={"note": note},
        clock=clock,
    )
    return row


def active(session: Session) -> list[AsnLimit]:
    return list(
        session.execute(
            select(AsnLimit).where(AsnLimit.lifted_at.is_(None)).order_by(AsnLimit.created_at)
        ).scalars()
    )


def active_factors(session: Session) -> dict[int, tuple[float, float]]:
    """{asn: (factor, asn_rps)} for the politeness policy."""
    return {row.asn: (row.factor, row.asn_rps) for row in active(session)}


def recent_lifted(session: Session, *, limit: int = 20) -> list[AsnLimit]:
    return list(
        session.execute(
            select(AsnLimit)
            .where(AsnLimit.lifted_at.is_not(None))
            .order_by(AsnLimit.lifted_at.desc())
            .limit(limit)
        ).scalars()
    )


def ingest(
    session: Session,
    items: list[ComplaintInput],
    *,
    actor: str,
    clock: Clock,
    table: AsnTable | None,
    factor: float,
    asn_rps: float,
) -> IngestResult:
    applied = unresolved = 0
    for c in items:
        try:
            record(
                session,
                target=c.target,
                source=c.source,
                note=c.note,
                actor=actor,
                clock=clock,
                table=table,
                factor=factor,
                asn_rps=asn_rps,
                received_at=c.received_at,
            )
        except ValidationError:
            unresolved += 1
            continue
        applied += 1
    return IngestResult(len(items), applied, unresolved)


def load_table(settings: Settings) -> AsnTable | None:
    """The ip2asn table named in `scan.asn_table_path`, or None when not configured."""
    path = settings.scan.asn_table_path
    return AsnTable.from_path(path) if path else None


def build_policy(
    settings: Settings, session_factory: Callable[[], Session], *, table: AsnTable | None = None
) -> AsnPolicy | None:
    """Politeness policy for the workers; None when no ip2asn table is configured."""
    table = table or load_table(settings)
    if table is None:
        return None

    def loader() -> dict[int, tuple[float, float]]:
        session = session_factory()
        try:
            return active_factors(session)
        finally:
            session.close()

    return AsnPolicy(table.lookup, loader, refresh_seconds=settings.scan.asn_limits_refresh_seconds)
