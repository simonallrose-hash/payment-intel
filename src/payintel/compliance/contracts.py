"""Contracts and entitlements (FR-KYC-04, FR-KYC-05, FR-KYC-07, AC-10)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.core import audit
from payintel.core.clock import Clock
from payintel.core.errors import C2DisabledError, ConflictError, NotFoundError, ValidationError
from payintel.core.flags import FlagService
from payintel.core.models.base import FieldProfile, Product, Role
from payintel.core.models.orgs import Contract, Entitlement, Organization
from payintel.core.settings import Settings
from payintel.entitlements.check import require_staff
from payintel.entitlements.model import Principal


@dataclass(frozen=True)
class EntitlementSpec:
    field_profile: FieldProfile
    countries: list[str] = field(default_factory=list)
    platforms: list[str] = field(default_factory=list)
    api_rps: int | None = None
    daily_records: int | None = None
    monthly_records: int | None = None
    export_max_rows: int | None = None
    export_schedule: str | None = None
    watchlist_limit: int | None = None
    allowed_ips: list[str] = field(default_factory=list)


def create_contract(
    session: Session,
    org: Organization,
    *,
    number: str,
    product: Product,
    starts_on: date,
    ends_on: date,
    allowed_purposes: list[str],
    file_key: str | None,
    principal: Principal,
    clock: Clock,
) -> Contract:
    require_staff(principal, Role.STAFF_COMPLIANCE, Role.STAFF_ADMIN)
    if ends_on < starts_on:
        raise ValidationError("ends_on before starts_on")
    if session.execute(select(Contract.id).where(Contract.number == number)).first():
        raise ConflictError("contract number exists", number=number)
    c = Contract(
        org_id=org.id,
        number=number,
        product=product,
        starts_on=starts_on,
        ends_on=ends_on,
        allowed_purposes=allowed_purposes,
        file_key=file_key,
        created_at=clock.now(),
    )
    session.add(c)
    session.flush()
    audit.record(
        session,
        actor=principal.actor,
        action="contract.create",
        object_type="contract",
        object_id=str(c.id),
        after={
            "org_id": str(org.id),
            "number": number,
            "product": product.value,
            "starts_on": starts_on.isoformat(),
            "ends_on": ends_on.isoformat(),
        },
        ip=principal.ip,
        clock=clock,
    )
    return c


def set_entitlement(
    session: Session,
    contract: Contract,
    spec: EntitlementSpec,
    *,
    principal: Principal,
    settings: Settings,
    flags: FlagService,
    clock: Clock,
) -> Entitlement:
    """Create or replace the contract's entitlement. `c2_risk` is refused while
    `feature_c2_enabled` is off (FR-KYC-07, LR-16)."""
    require_staff(principal, Role.STAFF_COMPLIANCE, Role.STAFF_ADMIN)
    if spec.field_profile == FieldProfile.C2_RISK and not flags.is_enabled("feature_c2_enabled"):
        raise C2DisabledError("c2_risk cannot be assigned: feature_c2_enabled is off")
    if spec.field_profile == FieldProfile.C2_RISK and contract.product != Product.C2:
        raise ValidationError("c2_risk requires a C2 contract")
    bad = [c for c in spec.countries if len(c) != 2 or not c.isalpha()]
    if bad:
        raise ValidationError("countries must be ISO alpha-2", countries=bad)
    ent = session.execute(
        select(Entitlement).where(Entitlement.contract_id == contract.id)
    ).scalar_one_or_none()
    before = None
    if ent is None:
        ent = Entitlement(
            contract_id=contract.id,
            field_profile=spec.field_profile,
            api_rps=0,
            daily_records=0,
            monthly_records=0,
            export_max_rows=0,
            watchlist_limit=0,
        )
        session.add(ent)
    else:
        before = _dict(ent)
    ent.field_profile = spec.field_profile
    ent.countries = [c.upper() for c in spec.countries]
    ent.platforms = list(spec.platforms)
    ent.api_rps = spec.api_rps if spec.api_rps is not None else settings.api.default_rps
    ent.daily_records = (
        spec.daily_records if spec.daily_records is not None else settings.api.default_daily_records
    )
    ent.monthly_records = (
        spec.monthly_records
        if spec.monthly_records is not None
        else settings.api.default_monthly_records
    )
    ent.export_max_rows = (
        spec.export_max_rows
        if spec.export_max_rows is not None
        else settings.export.default_max_rows
    )
    ent.export_schedule = spec.export_schedule or settings.export.default_schedule
    ent.watchlist_limit = (
        spec.watchlist_limit
        if spec.watchlist_limit is not None
        else settings.api.default_watchlist_limit
    )
    ent.allowed_ips = list(spec.allowed_ips)
    ent.active = True
    session.flush()
    audit.record(
        session,
        actor=principal.actor,
        action="entitlement.set",
        object_type="entitlement",
        object_id=str(contract.id),
        before=before,
        after=_dict(ent),
        ip=principal.ip,
        clock=clock,
    )
    return ent


def deactivate_entitlement(
    session: Session, ent: Entitlement, *, principal: Principal, clock: Clock
) -> None:
    require_staff(principal, Role.STAFF_COMPLIANCE, Role.STAFF_ADMIN)
    ent.active = False
    session.flush()
    audit.record(
        session,
        actor=principal.actor,
        action="entitlement.deactivate",
        object_type="entitlement",
        object_id=str(ent.contract_id),
        ip=principal.ip,
        clock=clock,
    )


def _dict(ent: Entitlement) -> dict[str, object]:
    return {
        "field_profile": ent.field_profile.value,
        "countries": list(ent.countries),
        "platforms": list(ent.platforms),
        "api_rps": ent.api_rps,
        "daily_records": ent.daily_records,
        "monthly_records": ent.monthly_records,
        "export_max_rows": ent.export_max_rows,
        "export_schedule": ent.export_schedule,
        "watchlist_limit": ent.watchlist_limit,
        "allowed_ips": list(ent.allowed_ips),
        "active": ent.active,
    }


def get_contract(session: Session, contract_id: uuid.UUID) -> Contract:
    c = session.get(Contract, contract_id)
    if c is None:
        raise NotFoundError("contract not found", id=str(contract_id))
    return c


def contracts_of(session: Session, org_id: uuid.UUID) -> list[tuple[Contract, Entitlement | None]]:
    rows = session.execute(
        select(Contract, Entitlement)
        .outerjoin(Entitlement, Entitlement.contract_id == Contract.id)
        .where(Contract.org_id == org_id)
        .order_by(Contract.starts_on.desc())
    ).all()
    return [(c, e) for c, e in rows]
