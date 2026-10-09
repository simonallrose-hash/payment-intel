"""KYC dossier (FR-KYC-02, LR-13, LR-14). Sanctions screening is a manual mark in v1
(FR-KYC-03 priority S): result, source and date are recorded by `staff_compliance`."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.core import audit
from payintel.core.clock import Clock
from payintel.core.errors import ValidationError
from payintel.core.models.base import KycDecision, Role
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
    return rec


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
) -> KycRecord:
    require_staff(principal, Role.STAFF_COMPLIANCE, Role.STAFF_ADMIN)
    if result not in SANCTIONS_RESULTS:
        raise ValidationError("unknown sanctions result", result=result)
    rec = get_or_create(session, org)
    rec.sanctions_result = result
    rec.sanctions_source = source
    rec.sanctions_checked_at = checked_at
    session.flush()
    audit.record(
        session,
        actor=principal.actor,
        action="kyc.sanctions",
        object_type="kyc_record",
        object_id=str(org.id),
        after={"result": result, "source": source, "checked_at": checked_at.isoformat()},
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
) -> KycRecord:
    """A decision needs a filled dossier, a sanctions check that is not `match`, and a comment."""
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
    }


def org_id_of(rec: KycRecord) -> uuid.UUID:
    return rec.org_id
