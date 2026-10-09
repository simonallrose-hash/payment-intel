"""Resolution of a `Grant` and the checks every client request passes (NFR-S-04).

Order of checks, each one sufficient to deny:
organisation `active` → contract in term today → entitlement present and
active → `c2_risk` profile only with `feature_c2_enabled` → caller IP in the
entitlement list (when a list is set) → API-key scope / portal role.
"""

from __future__ import annotations

import ipaddress
import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.core.errors import ForbiddenError
from payintel.core.flags import FlagService
from payintel.core.models.base import FieldProfile, OrgStatus, Role
from payintel.core.models.orgs import Contract, Entitlement, Organization
from payintel.entitlements.model import DenialCode, Grant, Principal


class EntitlementDenied(ForbiddenError):
    """403 with a machine-readable `reason` (FR-API-06)."""

    def __init__(self, reason: str, message: str = "", **context: object) -> None:
        super().__init__(message or reason, reason=reason, **context)
        self.reason = reason


def active_contract(session: Session, org_id: uuid.UUID, today: date) -> Contract | None:
    """The contract in term today with an active entitlement, newest start first."""
    rows = session.execute(
        select(Contract)
        .join(Entitlement, Entitlement.contract_id == Contract.id)
        .where(
            Contract.org_id == org_id,
            Contract.starts_on <= today,
            Contract.ends_on >= today,
            Entitlement.active.is_(True),
        )
        .order_by(Contract.starts_on.desc(), Contract.created_at.desc())
    ).scalars()
    return rows.first()


def resolve_grant(session: Session, org_id: uuid.UUID, *, today: date, flags: FlagService) -> Grant:
    org = session.get(Organization, org_id)
    if org is None or org.status != OrgStatus.ACTIVE:
        raise EntitlementDenied(DenialCode.ORG_NOT_ACTIVE, "organisation is not active")
    if org.restricted_at is not None:
        raise EntitlementDenied(
            DenialCode.ORG_RESTRICTED,
            "organisation access is restricted pending an abuse review (FR-AB-03)",
        )
    contract = active_contract(session, org_id, today)
    if contract is None:
        # Distinguish "no contract today" from "contract without an active entitlement".
        in_term = session.execute(
            select(Contract.id)
            .where(
                Contract.org_id == org_id,
                Contract.starts_on <= today,
                Contract.ends_on >= today,
            )
            .limit(1)
        ).first()
        if in_term:
            raise EntitlementDenied(
                DenialCode.NO_ENTITLEMENT, "contract in term has no active entitlement"
            )
        any_contract = session.execute(
            select(Contract.id).where(Contract.org_id == org_id).limit(1)
        ).first()
        raise EntitlementDenied(
            DenialCode.CONTRACT_NOT_IN_TERM if any_contract else DenialCode.NO_ENTITLEMENT,
            "no contract in term",
        )
    ent = session.execute(
        select(Entitlement).where(Entitlement.contract_id == contract.id)
    ).scalar_one()
    if ent.field_profile == FieldProfile.C2_RISK and not flags.is_enabled("feature_c2_enabled"):
        raise EntitlementDenied(DenialCode.C2_DISABLED, "c2_risk profile is disabled")
    return Grant(
        org_id=org_id,
        contract_id=contract.id,
        product=contract.product.value,
        profile=ent.field_profile,
        countries=frozenset(c.upper() for c in ent.countries),
        platforms=frozenset(ent.platforms),
        api_rps=ent.api_rps,
        daily_records=ent.daily_records,
        monthly_records=ent.monthly_records,
        export_max_rows=ent.export_max_rows,
        export_schedule=ent.export_schedule,
        watchlist_limit=ent.watchlist_limit,
        allowed_ips=tuple(ent.allowed_ips),
        contract_ends_on=contract.ends_on,
        allowed_purposes=tuple(contract.allowed_purposes),
    )


def ip_allowed(ip: str | None, allowed: tuple[str, ...] | list[str]) -> bool:
    """Empty list = no restriction. Entries are addresses or CIDR networks."""
    if not allowed:
        return True
    if ip is None:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for entry in allowed:
        try:
            net = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            continue
        if addr in net:
            return True
    return False


def check_ip(principal: Principal, grant: Grant) -> None:
    if not ip_allowed(principal.ip, grant.allowed_ips):
        raise EntitlementDenied(DenialCode.IP_NOT_ALLOWED, "caller IP is not in the allow-list")


def require_scope(principal: Principal, scope: str) -> None:
    if not principal.has_scope(scope):
        raise EntitlementDenied(DenialCode.SCOPE_MISSING, f"scope {scope} required", scope=scope)


def require_role(principal: Principal, role: Role) -> None:
    """Portal/API role floor: `org_viewer` < `org_analyst` < `org_admin`; staff passes."""
    if not principal.role_at_least(role):
        raise EntitlementDenied(DenialCode.ROLE_FORBIDDEN, f"role {role.value} required")


def require_staff(principal: Principal, *roles: Role) -> None:
    """Staff-only operation; with `roles` only those staff roles pass (3.2)."""
    if not principal.is_staff or (roles and principal.role not in roles):
        raise EntitlementDenied(DenialCode.ROLE_FORBIDDEN, "staff role required")


def require_same_org(principal: Principal, org_id: uuid.UUID) -> None:
    if principal.is_staff:
        return
    if principal.org_id != org_id:
        raise EntitlementDenied(DenialCode.OTHER_ORG, "object belongs to another organisation")


def require_profile(grant: Grant, *allowed: FieldProfile) -> None:
    if grant.profile not in allowed:
        raise EntitlementDenied(DenialCode.FIELD_PROFILE, "field profile does not allow this")
