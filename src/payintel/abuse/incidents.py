"""Incidents for `staff_compliance` and automatic restriction (FR-AB-03)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.abuse.detectors import Finding, detect_org
from payintel.core import audit
from payintel.core.clock import Clock
from payintel.core.errors import NotFoundError, ValidationError
from payintel.core.models.abuse import AbuseIncident
from payintel.core.models.base import OrgStatus
from payintel.core.models.orgs import Organization
from payintel.core.settings import AbuseSettings

STATUSES: tuple[str, ...] = ("open", "resolved", "dismissed")


@dataclass(frozen=True)
class RunResult:
    organisations: int
    incidents: list[AbuseIncident]
    restricted: list[uuid.UUID]


def open_incidents(session: Session, org_id: uuid.UUID | None = None) -> list[AbuseIncident]:
    q = select(AbuseIncident).where(AbuseIncident.status == "open")
    if org_id is not None:
        q = q.where(AbuseIncident.org_id == org_id)
    return list(session.execute(q.order_by(AbuseIncident.created_at.desc())).scalars())


def record(
    session: Session,
    org_id: uuid.UUID,
    finding: Finding,
    *,
    now: datetime,
    s: AbuseSettings,
    clock: Clock,
    actor: str = "system:abuse",
) -> AbuseIncident | None:
    """One open incident per (org, detector); escalate severity in place. Critical → restrict."""
    existing = session.execute(
        select(AbuseIncident).where(
            AbuseIncident.org_id == org_id,
            AbuseIncident.detector == finding.detector,
            AbuseIncident.status == "open",
        )
    ).scalar_one_or_none()
    created: AbuseIncident | None = None
    if existing is None:
        existing = AbuseIncident(
            org_id=org_id,
            detector=finding.detector,
            severity=finding.severity,
            summary=finding.summary[:512],
            details=finding.details,
            status="open",
            created_at=now,
        )
        session.add(existing)
        session.flush()
        created = existing
        audit.record(
            session,
            actor=actor,
            action="abuse.incident",
            object_type="abuse_incident",
            object_id=str(existing.id),
            after={
                "org_id": str(org_id),
                "detector": finding.detector,
                "severity": finding.severity,
            },
            clock=clock,
        )
    elif _rank(finding.severity) > _rank(existing.severity):
        existing.severity = finding.severity
        existing.summary = finding.summary[:512]
        existing.details = finding.details
        session.flush()
    if finding.severity == "critical" and s.auto_restrict:
        org = session.get(Organization, org_id)
        if org is not None and org.restricted_at is None:
            restrict(
                session,
                org,
                reason=f"auto: {finding.detector} ({finding.summary[:120]})",
                actor=actor,
                clock=clock,
            )
            existing.auto_restricted = True
            session.flush()
    return created


def _rank(sev: str) -> int:
    return {"low": 0, "medium": 1, "high": 2, "critical": 3}.get(sev, 0)


def run_all(session: Session, *, now: datetime, s: AbuseSettings, clock: Clock) -> RunResult:
    orgs = list(
        session.execute(
            select(Organization).where(Organization.status == OrgStatus.ACTIVE)
        ).scalars()
    )
    incidents: list[AbuseIncident] = []
    restricted: list[uuid.UUID] = []
    for org in orgs:
        was_restricted = org.restricted_at is not None
        for finding in detect_org(session, org.id, now=now, s=s):
            inc = record(session, org.id, finding, now=now, s=s, clock=clock)
            if inc is not None:
                incidents.append(inc)
        session.refresh(org)
        if not was_restricted and org.restricted_at is not None:
            restricted.append(org.id)
    return RunResult(len(orgs), incidents, restricted)


def restrict(session: Session, org: Organization, *, reason: str, actor: str, clock: Clock) -> None:
    """Refuse every API key of the organisation until compliance lifts the restriction."""
    now = clock.now()
    org.restricted_at = now
    org.restricted_reason = reason[:256]
    session.flush()
    audit.record(
        session,
        actor=actor,
        action="organization.restrict",
        object_type="organization",
        object_id=str(org.id),
        after={"reason": org.restricted_reason},
        clock=clock,
    )


def unrestrict(session: Session, org: Organization, *, actor: str, clock: Clock) -> None:
    before = {"reason": org.restricted_reason, "restricted_at": str(org.restricted_at)}
    org.restricted_at = None
    org.restricted_reason = None
    session.flush()
    audit.record(
        session,
        actor=actor,
        action="organization.unrestrict",
        object_type="organization",
        object_id=str(org.id),
        before=before,
        clock=clock,
    )


def resolve(
    session: Session,
    incident_id: int,
    *,
    status: str,
    resolution: str | None,
    lift_restriction: bool,
    actor: str,
    ip: str | None,
    clock: Clock,
) -> AbuseIncident:
    if status not in ("resolved", "dismissed"):
        raise ValidationError("status must be resolved or dismissed")
    inc = session.get(AbuseIncident, incident_id)
    if inc is None:
        raise NotFoundError("incident not found", id=incident_id)
    if inc.status != "open":
        raise ValidationError("incident is already closed")
    now = clock.now()
    inc.status = status
    inc.resolved_at = now
    inc.resolved_by = actor
    inc.resolution = resolution
    session.flush()
    audit.record(
        session,
        actor=actor,
        action="abuse.resolve",
        object_type="abuse_incident",
        object_id=str(inc.id),
        after={"status": status, "resolution": (resolution or "")[:200]},
        ip=ip,
        clock=clock,
    )
    if lift_restriction:
        org = session.get(Organization, inc.org_id)
        if (
            org is not None
            and org.restricted_at is not None
            and not open_incidents(session, org.id)
        ):
            unrestrict(session, org, actor=actor, clock=clock)
    return inc


def incident_dict(inc: AbuseIncident) -> dict[str, Any]:
    return {
        "id": inc.id,
        "org_id": str(inc.org_id),
        "detector": inc.detector,
        "severity": inc.severity,
        "summary": inc.summary,
        "status": inc.status,
        "created_at": inc.created_at.isoformat(),
    }
