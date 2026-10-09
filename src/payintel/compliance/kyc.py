"""KYC dossier (FR-KYC-02, LR-13, LR-14) and re-KYC schedule (FR-KYC-06).

Sanctions screening (FR-KYC-03) is either recorded by hand by `staff_compliance`
(result, source, date) or written by `compliance.sanctions.screen` from the
OpenSanctions matching API. An approval starts a 12-month review cycle; a
change of beneficial owners makes the review due at once. `remind` issues the
30-day reminder once per due date (audit row + optional Telegram message).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.core import audit
from payintel.core.clock import Clock
from payintel.core.errors import ValidationError
from payintel.core.models.base import KycDecision, OrgStatus, Role
from payintel.core.models.orgs import KycRecord, Organization
from payintel.entitlements.check import require_staff
from payintel.entitlements.model import Principal

PURPOSES: tuple[str, ...] = (
    "market_research",
    "sales_prospecting",
    "partnership_analysis",
    "investment_analysis",
    "competitive_analysis",
)
SANCTIONS_RESULTS: tuple[str, ...] = ("clear", "potential_match", "match")
REVIEW_INTERVAL_DAYS = 365
REMINDER_DAYS = 30


@dataclass
class Dossier:
    address: str | None = None
    website: str | None = None
    beneficiaries: list[dict[str, Any]] = field(default_factory=list)
    contact_name: str | None = None
    contact_title: str | None = None
    purpose: str | None = None


def get_or_create(session: Session, org: Organization) -> KycRecord:
    rec = session.execute(select(KycRecord).where(KycRecord.org_id == org.id)).scalar_one_or_none()
    if rec is None:
        rec = KycRecord(org_id=org.id)
        session.add(rec)
        session.flush()
    return rec


def _check_beneficiaries(items: list[dict[str, Any]]) -> None:
    for b in items:
        if "name" not in b or not str(b["name"]).strip():
            raise ValidationError("beneficiary needs a name")
        share = float(b.get("share", 0))
        if share < 25 or share > 100:
            raise ValidationError("beneficiaries are persons holding ≥25 %", share=share)


def update_dossier(
    session: Session, org: Organization, d: Dossier, *, principal: Principal, clock: Clock
) -> KycRecord:
    require_staff(principal, Role.STAFF_COMPLIANCE, Role.STAFF_ADMIN)
    if d.purpose is not None and d.purpose not in PURPOSES:
        raise ValidationError("purpose must be from the closed list", purpose=d.purpose)
    _check_beneficiaries(d.beneficiaries)
    rec = get_or_create(session, org)
    before = {"purpose": rec.purpose, "beneficiaries": rec.beneficiaries}
    owners_changed = _owners(rec.beneficiaries) != _owners(d.beneficiaries)
    rec.address = d.address
    rec.website = d.website
    rec.beneficiaries = d.beneficiaries
    rec.contact_name = d.contact_name
    rec.contact_title = d.contact_title
    rec.purpose = d.purpose
    session.flush()
    audit.record(
        session,
        actor=principal.actor,
        action="kyc.update",
        object_type="kyc_record",
        object_id=str(org.id),
        before=before,
        after={"purpose": rec.purpose, "beneficiaries": rec.beneficiaries},
        ip=principal.ip,
        clock=clock,
    )
    if owners_changed and rec.decision == KycDecision.APPROVED:
        # FR-KYC-06: a change of beneficial owners triggers a re-KYC at once
        rec.next_review_at = clock.now()
        rec.reminder_sent_at = None
        session.flush()
        audit.record(
            session,
            actor=principal.actor,
            action="kyc.review_due",
            object_type="kyc_record",
            object_id=str(org.id),
            after={"reason": "beneficiaries_changed", "due": rec.next_review_at.isoformat()},
            ip=principal.ip,
            clock=clock,
        )
    return rec


def _owners(items: list[dict[str, Any]]) -> set[tuple[str, float]]:
    return {(str(b.get("name", "")).strip().lower(), float(b.get("share", 0))) for b in items}


def add_document(
    session: Session, org: Organization, key: str, *, principal: Principal, clock: Clock
) -> KycRecord:
    require_staff(principal, Role.STAFF_COMPLIANCE, Role.STAFF_ADMIN)
    rec = get_or_create(session, org)
    rec.documents = [*rec.documents, key]
    session.flush()
    audit.record(
        session,
        actor=principal.actor,
        action="kyc.document",
        object_type="kyc_record",
        object_id=str(org.id),
        after={"document": key},
        ip=principal.ip,
        clock=clock,
    )
    return rec


def record_sanctions(
    session: Session,
    org: Organization,
    *,
    result: str,
    source: str,
    checked_at: datetime,
    principal: Principal,
    clock: Clock,
    details: list[dict[str, Any]] | None = None,
) -> KycRecord:
    require_staff(principal, Role.STAFF_COMPLIANCE, Role.STAFF_ADMIN)
    if result not in SANCTIONS_RESULTS:
        raise ValidationError("unknown sanctions result", result=result)
    if not source.strip():
        raise ValidationError("a source is required")
    rec = get_or_create(session, org)
    rec.sanctions_result = result
    rec.sanctions_source = source
    rec.sanctions_checked_at = checked_at
    rec.sanctions_details = details
    session.flush()
    audit.record(
        session,
        actor=principal.actor,
        action="kyc.sanctions",
        object_type="kyc_record",
        object_id=str(org.id),
        after={
            "result": result,
            "source": source,
            "checked_at": checked_at.isoformat(),
            "hits": len(details or []),
        },
        ip=principal.ip,
        clock=clock,
    )
    return rec


def record_video_call(
    session: Session, org: Organization, *, at: datetime, principal: Principal, clock: Clock
) -> KycRecord:
    require_staff(principal, Role.STAFF_COMPLIANCE, Role.STAFF_ADMIN)
    rec = get_or_create(session, org)
    rec.video_call_at = at
    session.flush()
    return rec


def decide(
    session: Session,
    org: Organization,
    decision: KycDecision,
    *,
    principal: Principal,
    comment: str,
    clock: Clock,
    review_interval_days: int = REVIEW_INTERVAL_DAYS,
) -> KycRecord:
    """A decision needs a filled dossier, a sanctions check that is not `match`, and a comment.

    An approval schedules the next review `review_interval_days` later (FR-KYC-06).
    """
    require_staff(principal, Role.STAFF_COMPLIANCE, Role.STAFF_ADMIN)
    if not comment.strip():
        raise ValidationError("a comment is required")
    rec = get_or_create(session, org)
    if decision == KycDecision.APPROVED:
        missing = [
            k
            for k, v in {
                "purpose": rec.purpose,
                "contact_name": rec.contact_name,
                "beneficiaries": rec.beneficiaries,
                "sanctions_result": rec.sanctions_result,
                "reg_number": org.reg_number,
            }.items()
            if not v
        ]
        if missing:
            raise ValidationError("dossier incomplete", missing=missing)
        if rec.sanctions_result == "match":
            raise ValidationError("sanctions match: approval not possible (LR-14)")
    rec.decision = decision
    rec.decided_by = principal.user_id
    rec.decided_at = clock.now()
    rec.decision_comment = comment.strip()
    rec.reminder_sent_at = None
    rec.next_review_at = (
        rec.decided_at + timedelta(days=review_interval_days)
        if decision == KycDecision.APPROVED
        else None
    )
    session.flush()
    audit.record(
        session,
        actor=principal.actor,
        action="kyc.decide",
        object_type="kyc_record",
        object_id=str(org.id),
        after={"decision": decision.value, "comment": comment.strip()},
        ip=principal.ip,
        clock=clock,
    )
    return rec


def dossier_dict(rec: KycRecord | None) -> dict[str, Any]:
    if rec is None:
        return {}
    return {
        "address": rec.address,
        "website": rec.website,
        "beneficiaries": rec.beneficiaries,
        "contact_name": rec.contact_name,
        "contact_title": rec.contact_title,
        "purpose": rec.purpose,
        "documents": list(rec.documents),
        "sanctions_result": rec.sanctions_result,
        "sanctions_source": rec.sanctions_source,
        "sanctions_checked_at": rec.sanctions_checked_at,
        "video_call_at": rec.video_call_at,
        "decision": rec.decision.value if rec.decision else None,
        "decided_by": str(rec.decided_by) if rec.decided_by else None,
        "decided_at": rec.decided_at,
        "decision_comment": rec.decision_comment,
        "sanctions_details": list(rec.sanctions_details or []),
        "next_review_at": rec.next_review_at,
        "reminder_sent_at": rec.reminder_sent_at,
    }


# --- re-KYC (FR-KYC-06) -------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewDue:
    org: Organization
    rec: KycRecord
    due_at: datetime
    overdue: bool
    reminded: bool


def due_for_review(
    session: Session, *, now: datetime, within_days: int = REMINDER_DAYS
) -> list[ReviewDue]:
    """Active or approved organisations whose review is due within `within_days` (or past)."""
    horizon = now + timedelta(days=within_days)
    rows = session.execute(
        select(Organization, KycRecord)
        .join(KycRecord, KycRecord.org_id == Organization.id)
        .where(
            KycRecord.next_review_at.is_not(None),
            KycRecord.next_review_at <= horizon,
            Organization.status.in_([OrgStatus.ACTIVE, OrgStatus.APPROVED, OrgStatus.SUSPENDED]),
        )
        .order_by(KycRecord.next_review_at, Organization.legal_name)
    ).all()
    out: list[ReviewDue] = []
    for org, rec in rows:
        assert rec.next_review_at is not None  # noqa: S101 - filtered above
        out.append(
            ReviewDue(
                org,
                rec,
                rec.next_review_at,
                rec.next_review_at <= now,
                rec.reminder_sent_at is not None,
            )
        )
    return out


def remind(
    session: Session,
    *,
    now: datetime,
    clock: Clock,
    within_days: int = REMINDER_DAYS,
    notify: Callable[[str], None] | None = None,
    actor: str = "system:rekyc",
) -> list[ReviewDue]:
    """Issue the reminder once per due date: audit row, optional message to staff."""
    sent: list[ReviewDue] = []
    for item in due_for_review(session, now=now, within_days=within_days):
        if item.reminded:
            continue
        item.rec.reminder_sent_at = now
        session.flush()
        state = "overdue" if item.overdue else f"due {item.due_at:%Y-%m-%d}"
        audit.record(
            session,
            actor=actor,
            action="kyc.review_reminder",
            object_type="kyc_record",
            object_id=str(item.org.id),
            after={"due": item.due_at.isoformat(), "overdue": item.overdue},
            clock=clock,
        )
        if notify is not None:
            notify(f"re-KYC {state}: {item.org.legal_name} ({item.org.id})")
        sent.append(item)
    return sent


def start_review(
    session: Session, org: Organization, *, principal: Principal, clock: Clock
) -> KycRecord:
    """Open the re-KYC: the previous decision and sanctions result are cleared, the dossier
    and documents stay; the organisation keeps its status until compliance decides."""
    require_staff(principal, Role.STAFF_COMPLIANCE, Role.STAFF_ADMIN)
    rec = get_or_create(session, org)
    if rec.decision is None:
        raise ValidationError("no decision to review yet")
    before = {
        "decision": rec.decision.value,
        "decided_at": rec.decided_at.isoformat() if rec.decided_at else None,
        "sanctions_result": rec.sanctions_result,
    }
    rec.decision = None
    rec.decided_by = None
    rec.decided_at = None
    rec.decision_comment = None
    rec.sanctions_result = None
    rec.next_review_at = None
    rec.reminder_sent_at = None
    session.flush()
    audit.record(
        session,
        actor=principal.actor,
        action="kyc.review_start",
        object_type="kyc_record",
        object_id=str(org.id),
        before=before,
        ip=principal.ip,
        clock=clock,
    )
    return rec


def org_id_of(rec: KycRecord) -> uuid.UUID:
    return rec.org_id
