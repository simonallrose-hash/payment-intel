"""Canary monitoring (FR-AB-04).

Every export carries 3–10 synthetic domains under the company's canary zone
(`exports/canary.py`). Any access to such a domain — a DNS query seen by the
authoritative resolver, an HTTP request on the catch-all web host, a mail to
an address under it — is fed here from the respective logs. A hit is stored,
linked to the organisation that received that export, and raises a `high`
incident for `staff_compliance`.

Input lines: `<timestamp> <domain-or-address> [source] [detail…]`; a line
starting with `#` is ignored. Timestamps are ISO 8601 (`Z` or offset).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.abuse import incidents
from payintel.abuse.detectors import Finding
from payintel.core.clock import Clock
from payintel.core.models.abuse import CanaryHit
from payintel.core.models.exports import Canary
from payintel.core.settings import AbuseSettings

KINDS: tuple[str, ...] = ("dns", "http", "email")


@dataclass(frozen=True)
class HitInput:
    observed_at: datetime
    name: str  # domain, hostname under it, or e-mail address
    source: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class IngestResult:
    lines: int
    matched: int
    unmatched: int
    incidents: int


def parse_lines(lines: list[str]) -> list[HitInput]:
    out: list[HitInput] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(maxsplit=3)
        if len(parts) < 2:
            continue
        ts = datetime.fromisoformat(parts[0].replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        out.append(
            HitInput(
                observed_at=ts,
                name=parts[1].lower().rstrip("."),
                source=parts[2] if len(parts) > 2 else None,
                detail=parts[3][:512] if len(parts) > 3 else None,
            )
        )
    return out


def canary_domain_of(name: str, zone: str) -> str | None:
    """`c-abc.zone`, `www.c-abc.zone` or `x@c-abc.zone` → the canary domain `c-abc.zone`."""
    host = name.split("@", 1)[-1].lower().rstrip(".")
    suffix = "." + zone.lower()
    if not host.endswith(suffix):
        return None
    labels = host[: -len(suffix)].split(".")
    return f"{labels[-1]}{suffix}" if labels and labels[-1] else None


def ingest(
    session: Session,
    hits: list[HitInput],
    *,
    kind: str,
    zone: str,
    s: AbuseSettings,
    clock: Clock,
) -> IngestResult:
    if kind not in KINDS:
        raise ValueError("kind must be dns, http or email")
    now = clock.now()
    matched = unmatched = created = 0
    for hit in hits:
        domain = canary_domain_of(hit.name, zone)
        canary = (
            session.execute(select(Canary).where(Canary.domain == domain)).scalar_one_or_none()
            if domain
            else None
        )
        if canary is None:
            unmatched += 1
            continue
        matched += 1
        session.add(
            CanaryHit(
                canary_id=canary.id,
                org_id=canary.org_id,
                kind=kind,
                source=(hit.source or "")[:256] or None,
                detail=hit.detail,
                observed_at=hit.observed_at,
                recorded_at=now,
            )
        )
        if canary.last_hit_at is None or hit.observed_at > canary.last_hit_at:
            canary.last_hit_at = hit.observed_at
        session.flush()
        inc = incidents.record(
            session,
            canary.org_id,
            Finding(
                "canary_hit",
                "high",
                f"{kind} access to canary {canary.domain} from {hit.source or 'unknown'}",
                {
                    "canary_id": canary.id,
                    "domain": canary.domain,
                    "export_job_id": str(canary.export_job_id) if canary.export_job_id else None,
                    "kind": kind,
                },
            ),
            now=now,
            s=s,
            clock=clock,
            actor="system:canary",
        )
        created += inc is not None
    return IngestResult(len(hits), matched, unmatched, created)


def recent_hits(session: Session, *, limit: int = 100) -> list[tuple[CanaryHit, Canary]]:
    rows = session.execute(
        select(CanaryHit, Canary)
        .join(Canary, Canary.id == CanaryHit.canary_id)
        .order_by(CanaryHit.observed_at.desc())
        .limit(limit)
    ).all()
    return [(h, c) for h, c in rows]
