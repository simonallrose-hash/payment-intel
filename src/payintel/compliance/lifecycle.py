"""Organisation lifecycle (FR-KYC-01, 3.2 separation of duties, AC-10).

`applied → kyc_in_progress → approved | rejected → active → suspended → terminated`
(plus `suspended → active`). Every transition needs a staff role from the
table below and a non-empty comment, and is written to the audit log with
the comment. Activation additionally requires an approved KYC dossier.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.core import audit
from payintel.core.clock import Clock
from payintel.core.errors import ConflictError, ValidationError
from payintel.core.models.base import KycDecision, OrgStatus, Role
from payintel.core.models.orgs import KycRecord, Organization
from payintel.entitlements.check import EntitlementDenied
from payintel.entitlements.model import DenialCode, Principal

TRANSITIONS: dict[OrgStatus, frozenset[OrgStatus]] = {
    OrgStatus.APPLIED: frozenset({OrgStatus.KYC_IN_PROGRESS, OrgStatus.REJECTED}),
    OrgStatus.KYC_IN_PROGRESS: frozenset({OrgStatus.APPROVED, OrgStatus.REJECTED}),
    OrgStatus.APPROVED: frozenset({OrgStatus.ACTIVE, OrgStatus.REJECTED}),
    OrgStatus.REJECTED: frozenset({OrgStatus.KYC_IN_PROGRESS}),
    OrgStatus.ACTIVE: frozenset({OrgStatus.SUSPENDED, OrgStatus.TERMINATED}),
    OrgStatus.SUSPENDED: frozenset({OrgStatus.ACTIVE, OrgStatus.TERMINATED}),
    OrgStatus.TERMINATED: frozenset(),
}

# Who may move an organisation into a status (3.2): activation, suspension and
# termination are compliance/admin only; KYC steps also by compliance/admin.
ALLOWED_ROLES: dict[OrgStatus, frozenset[Role]] = {
    OrgStatus.KYC_IN_PROGRESS: frozenset({Role.STAFF_COMPLIANCE, Role.STAFF_ADMIN}),
    OrgStatus.APPROVED: frozenset({Role.STAFF_COMPLIANCE, Role.STAFF_ADMIN}),
    OrgStatus.REJECTED: frozenset({Role.STAFF_COMPLIANCE, Role.STAFF_ADMIN}),
    OrgStatus.ACTIVE: frozenset({Role.STAFF_COMPLIANCE, Role.STAFF_ADMIN}),
    OrgStatus.SUSPENDED: frozenset({Role.STAFF_COMPLIANCE, Role.STAFF_ADMIN}),
    OrgStatus.TERMINATED: frozenset({Role.STAFF_COMPLIANCE, Role.STAFF_ADMIN}),
}


def kyc_approved(session: Session, org_id: object) -> bool:
    rec = session.execute(select(KycRecord).where(KycRecord.org_id == org_id)).scalar_one_or_none()
    return rec is not None and rec.decision == KycDecision.APPROVED


def apply(
    session: Session,
    *,
    legal_name: str,
    reg_number: str | None,
    country: str,
    actor: str,
    clock: Clock,
) -> Organization:
    """UC-01: a new organisation in `applied`."""
    if not legal_name.strip():
        raise ValidationError("legal_name is required")
    org = Organization(
        legal_name=legal_name.strip(),
        reg_number=reg_number,
        country=country.upper(),
        status=OrgStatus.APPLIED,
        created_at=clock.now(),
    )
    session.add(org)
    session.flush()
    audit.record(
        session,
        actor=actor,
        action="organization.apply",
        object_type="organization",
        object_id=str(org.id),
        after={"legal_name": org.legal_name, "country": org.country, "status": "applied"},
        clock=clock,
    )
    return org


def transition(
    session: Session,
    org: Organization,
    to: OrgStatus,
    *,
    principal: Principal,
    comment: str,
    clock: Clock,
) -> Organization:
    if not comment or not comment.strip():
        raise ValidationError("a comment is required for every status change")
    if to not in TRANSITIONS[org.status]:
        raise ConflictError(
            f"transition {org.status.value} → {to.value} is not allowed", status=org.status.value
        )
    if principal.role not in ALLOWED_ROLES.get(to, frozenset()):
        raise EntitlementDenied(
            DenialCode.ROLE_FORBIDDEN, f"role {principal.role.value} cannot set {to.value}"
        )
    if to in (OrgStatus.APPROVED, OrgStatus.ACTIVE) and not kyc_approved(session, org.id):
        raise ConflictError(f"{to.value} requires an approved KYC dossier (FR-KYC-01)")
    before = org.status.value
    org.status = to
    session.flush()
    audit.record(
        session,
        actor=principal.actor,
        action="organization.transition",
        object_type="organization",
        object_id=str(org.id),
        before={"status": before},
        after={"status": to.value, "comment": comment.strip()},
        ip=principal.ip,
        clock=clock,
    )
    return org
