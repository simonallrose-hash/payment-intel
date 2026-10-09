"""`GET /v1/providers`, `GET /v1/payment-methods` (FR-API-03)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from payintel.api.deps import Access, SessionDep, StateDep, scoped
from payintel.api.schemas.common import Page, PaymentMethodOut, ProviderOut
from payintel.core.models.base import ReferenceStatus
from payintel.core.models.reference import PaymentMethod, Provider

router = APIRouter(prefix="/v1", tags=["reference"])
RefAccess = Annotated[Access, Depends(scoped("stores:read"))]


@router.get("/providers", response_model=Page[ProviderOut])
def providers(request: Request, state: StateDep, session: SessionDep, _a: RefAccess) -> Any:
    rows = session.execute(
        select(Provider).where(Provider.status != ReferenceStatus.DEPRECATED).order_by(Provider.id)
    ).scalars()
    items = [
        ProviderOut(
            id=p.id,
            name=p.name,
            role=p.role.value,
            owner_company=p.owner_company,
            countries=list(p.countries),
            website=p.website,
            status=p.status.value,
        )
        for p in rows
    ]
    request.state.records = 0
    return Page(
        items=items, as_of=state.clock.now(), methodology_url=state.settings.api.methodology_url
    )


@router.get("/payment-methods", response_model=Page[PaymentMethodOut])
def payment_methods(request: Request, state: StateDep, session: SessionDep, _a: RefAccess) -> Any:
    rows = session.execute(
        select(PaymentMethod)
        .where(PaymentMethod.status != ReferenceStatus.DEPRECATED)
        .order_by(PaymentMethod.id)
    ).scalars()
    items = [
        PaymentMethodOut(
            id=m.id,
            name=m.name,
            type=m.type.value,
            scheme_or_brand=m.scheme_or_brand,
            regions=list(m.regions),
            default_provider_id=m.default_provider_id,
        )
        for m in rows
    ]
    request.state.records = 0
    return Page(
        items=items, as_of=state.clock.now(), methodology_url=state.settings.api.methodology_url
    )
