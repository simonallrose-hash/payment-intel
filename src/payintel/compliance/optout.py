"""Opt-out with domain ownership proof (FR-OO-02, FR-DS-09, LR-06, AC-11).

1. The owner submits the domain on the public page and receives a token.
2. They publish `payintel-optout=<token>` as a DNS TXT record at the apex or
   as the body of `https://<domain>/.well-known/payintel-optout.txt`.
3. `verify` checks one of the two (through injectable lookups) and stamps
   `verified_at`. From that moment the entitlements lineage guard hides the
   domain from every API answer and export, and `apply` (run by the daily
   job or immediately after verification) removes scan plans and flags the
   domain, so the 72-hour bound (FR-OO-02) is met immediately.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.alerts.watchlists import normalise_domain
from payintel.core import audit
from payintel.core.clock import Clock
from payintel.core.errors import NotFoundError, ValidationError
from payintel.core.models.audit import OptoutRequest
from payintel.core.models.base import DomainStatus, OptoutMethod
from payintel.core.models.domains import Domain
from payintel.scheduler.planner import remove_optout_plans

TXT_PREFIX = "payintel-optout="
WELL_KNOWN_PATH = "/.well-known/payintel-optout.txt"

DnsTxtLookup = Callable[[str], list[str]]
HttpGet = Callable[[str], tuple[int, str]]


@dataclass(frozen=True)
class Instructions:
    token: str
    txt_record: str
    well_known_url: str


def request(
    session: Session, *, domain: str, method: OptoutMethod, contact: str | None, clock: Clock
) -> tuple[OptoutRequest, Instructions]:
    d = normalise_domain(domain)
    if d is None:
        raise ValidationError("invalid domain", domain=domain)
    token = secrets.token_urlsafe(24)
    req = OptoutRequest(
        domain=d, method=method, token=token, contact=contact, requested_at=clock.now()
    )
    session.add(req)
    session.flush()
    audit.record(
        session,
        actor="public:optout",
        action="optout.request",
        object_type="domain",
        object_id=d,
        after={"method": method.value, "request_id": req.id},
        clock=clock,
    )
    return req, instructions(req)


def instructions(req: OptoutRequest) -> Instructions:
    return Instructions(
        token=req.token,
        txt_record=f"{TXT_PREFIX}{req.token}",
        well_known_url=f"https://{req.domain}{WELL_KNOWN_PATH}",
    )


def get_by_token(session: Session, token: str) -> OptoutRequest:
    req = session.execute(
        select(OptoutRequest).where(OptoutRequest.token == token)
    ).scalar_one_or_none()
    if req is None:
        raise NotFoundError("opt-out request not found")
    return req


def verify(
    session: Session,
    req: OptoutRequest,
    *,
    dns_txt: DnsTxtLookup,
    http_get: HttpGet,
    clock: Clock,
) -> bool:
    """Check the proof for `req.method`; on success stamp `verified_at` and apply."""
    if req.verified_at is not None:
        return True
    ok = False
    if req.method == OptoutMethod.DNS_TXT:
        try:
            records = dns_txt(req.domain)
        except Exception:
            records = []
        ok = any(r.strip().strip('"') == f"{TXT_PREFIX}{req.token}" for r in records)
    else:
        try:
            status, body = http_get(f"https://{req.domain}{WELL_KNOWN_PATH}")
        except Exception:
            status, body = 0, ""
        ok = status == 200 and body.strip() == req.token
    if not ok:
        return False
    req.verified_at = clock.now()
    session.flush()
    audit.record(
        session,
        actor="system:optout",
        action="optout.verify",
        object_type="domain",
        object_id=req.domain,
        after={"method": req.method.value, "request_id": req.id},
        clock=clock,
    )
    apply_verified(session, clock=clock)
    return True


def apply_verified(session: Session, *, clock: Clock) -> int:
    """Flag verified domains, drop their scan plans, stamp `applied_at`. Idempotent."""
    pending = list(
        session.execute(
            select(OptoutRequest).where(
                OptoutRequest.verified_at.is_not(None), OptoutRequest.applied_at.is_(None)
            )
        ).scalars()
    )
    now = clock.now()
    for req in pending:
        domain = session.execute(
            select(Domain).where(Domain.etld1 == req.domain)
        ).scalar_one_or_none()
        if domain is not None:
            domain.optout = True
            domain.status = DomainStatus.OPTOUT
            domain.status_reason = f"optout request {req.id}"
            domain.status_changed_at = now
        req.applied_at = now
    session.flush()
    if pending:
        remove_optout_plans(session)
        audit.record(
            session,
            actor="system:optout",
            action="optout.apply",
            object_type="domain",
            object_id=",".join(r.domain for r in pending)[:128],
            after={"applied": len(pending)},
            clock=clock,
        )
    return len(pending)


def pending_requests(session: Session) -> list[OptoutRequest]:
    return list(
        session.execute(
            select(OptoutRequest)
            .where(OptoutRequest.verified_at.is_(None))
            .order_by(OptoutRequest.requested_at.desc())
        ).scalars()
    )


def manual_verify(session: Session, req: OptoutRequest, *, actor: str, clock: Clock) -> None:
    """`staff_compliance` confirms ownership out of band (e.g. signed letter)."""
    if req.verified_at is None:
        req.verified_at = clock.now()
        req.notes = f"{req.notes or ''}\nmanually verified by {actor}".strip()
        session.flush()
        audit.record(
            session,
            actor=actor,
            action="optout.verify_manual",
            object_type="domain",
            object_id=req.domain,
            after={"request_id": req.id},
            clock=clock,
        )
        apply_verified(session, clock=clock)
