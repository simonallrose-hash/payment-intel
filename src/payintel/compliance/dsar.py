"""GDPR data-subject requests with a 30-day journal (FR-OO-03, LR-07…LR-10)."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.core import audit
from payintel.core.clock import Clock
from payintel.core.errors import ValidationError
from payintel.core.models.base import DsarKind, DsarStatus, Role
from payintel.core.models.portal import DsarRequest
from payintel.entitlements.check import require_staff
from payintel.entitlements.model import Principal


def open_request(
    session: Session,
    *,
    kind: DsarKind,
    subject: str,
    contact: str,
    details: str | None,
    deadline_days: int,
    clock: Clock,
) -> DsarRequest:
    if not subject.strip() or not contact.strip():
        raise ValidationError("subject and contact are required")
    now = clock.now()
    req = DsarRequest(
        kind=kind,
        subject=subject.strip(),
        contact=contact.strip(),
        details=details,
        received_at=now,
        due_at=now + timedelta(days=deadline_days),
    )
    session.add(req)
    session.flush()
    audit.record(
        session,
        actor="public:dsar",
        action="dsar.open",
        object_type="dsar_request",
        object_id=str(req.id),
        after={"kind": kind.value, "due_at": req.due_at.isoformat()},
        clock=clock,
    )
    return req


def take(session: Session, req: DsarRequest, *, principal: Principal, clock: Clock) -> DsarRequest:
    require_staff(principal, Role.STAFF_COMPLIANCE, Role.STAFF_ADMIN)
    req.status = DsarStatus.IN_PROGRESS
    req.handled_by = principal.actor
    session.flush()
    return req


def close(
    session: Session, req: DsarRequest, *, principal: Principal, resolution: str, clock: Clock
) -> DsarRequest:
    require_staff(principal, Role.STAFF_COMPLIANCE, Role.STAFF_ADMIN)
    if not resolution.strip():
        raise ValidationError("a resolution is required")
    req.status = DsarStatus.CLOSED
    req.handled_by = principal.actor
    req.closed_at = clock.now()
    req.resolution = resolution.strip()
    session.flush()
    audit.record(
        session,
        actor=principal.actor,
        action="dsar.close",
        object_type="dsar_request",
        object_id=str(req.id),
        after={"resolution": req.resolution, "late": req.closed_at > req.due_at},
        ip=principal.ip,
        clock=clock,
    )
    return req


def open_requests(session: Session) -> list[DsarRequest]:
    return list(
        session.execute(
            select(DsarRequest)
            .where(DsarRequest.status != DsarStatus.CLOSED)
            .order_by(DsarRequest.due_at)
        ).scalars()
    )


def overdue(session: Session, *, clock: Clock) -> list[DsarRequest]:
    now = clock.now()
    return [r for r in open_requests(session) if r.due_at < now]
