"""`GET /v1/stores/{domain}`, `/history`, `GET /v1/stores` (FR-API-03/04/05)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request

from payintel.api import read
from payintel.api.deps import Access, SessionDep, StateDep, scoped
from payintel.api.schemas.c1_basic import ChangeBasic, StoreBasic
from payintel.api.schemas.c1_full import ChangeFull, StoreFull
from payintel.api.schemas.common import Page
from payintel.core.errors import ValidationError
from payintel.entitlements import profiles

router = APIRouter(prefix="/v1/stores", tags=["stores"])

StoresAccess = Annotated[Access, Depends(scoped("stores:read"))]


def _page_size(state: StateDep, limit: int | None) -> int:
    maximum = state.settings.api.max_page_size
    if limit is None:
        return min(100, maximum)
    if limit < 1 or limit > maximum:
        raise ValidationError(f"limit must be between 1 and {maximum}", limit=limit)
    return limit


@router.get("/{domain}", response_model=StoreBasic | StoreFull, response_model_exclude_none=False)
def get_store(
    domain: str, request: Request, state: StateDep, session: SessionDep, access: StoresAccess
) -> Any:
    row = read.lookup(session, access.grant, domain)
    request.state.records = 1
    return read.build_store(
        session,
        row,
        profile=access.grant.profile,
        methodology_url=state.settings.api.methodology_url,
        evidence=state.evidence,
    )


@router.get("/{domain}/history", response_model=Page[ChangeBasic | ChangeFull])
def get_history(
    domain: str,
    request: Request,
    state: StateDep,
    session: SessionDep,
    access: StoresAccess,
    cursor: str | None = None,
    limit: int | None = None,
) -> Any:
    row = read.lookup(session, access.grant, domain)
    size = _page_size(state, limit)
    items, nxt = read.history(session, row, profile=access.grant.profile, limit=size, cursor=cursor)
    request.state.records = len(items)
    return Page(
        items=items,
        next_cursor=nxt,
        as_of=state.clock.now(),
        methodology_url=state.settings.api.methodology_url,
    )


@router.get("", response_model=Page[StoreBasic | StoreFull])
def search_stores(
    request: Request,
    state: StateDep,
    session: SessionDep,
    access: StoresAccess,
    country: Annotated[list[str], Query()] = [],  # noqa: B006 - FastAPI query default
    platform: Annotated[list[str], Query()] = [],  # noqa: B006
    provider: Annotated[list[str], Query()] = [],  # noqa: B006
    without_provider: Annotated[list[str], Query()] = [],  # noqa: B006
    provider_role: Annotated[list[str], Query()] = [],  # noqa: B006
    payment_method: Annotated[list[str], Query()] = [],  # noqa: B006
    providers_min: int | None = None,
    providers_max: int | None = None,
    min_confidence: str | None = None,
    first_seen_from: date | None = None,
    first_seen_to: date | None = None,
    last_seen_from: date | None = None,
    last_seen_to: date | None = None,
    changed_from: datetime | None = None,
    changed_to: datetime | None = None,
    domain_prefix: str | None = None,
    sort: str = "domain",
    cursor: str | None = None,
    limit: int | None = None,
) -> Any:
    if min_confidence is not None and min_confidence not in ("high", "medium", "low"):
        raise ValidationError("min_confidence must be high|medium|low")
    filters = read.StoreFilters(
        countries=country,
        platforms=platform,
        providers=provider,
        without_providers=without_provider,
        provider_roles=provider_role,
        methods=payment_method,
        providers_min=providers_min,
        providers_max=providers_max,
        min_confidence=min_confidence,
        first_seen_from=first_seen_from,
        first_seen_to=first_seen_to,
        last_seen_from=last_seen_from,
        last_seen_to=last_seen_to,
        changed_from=changed_from,
        changed_to=changed_to,
        domain_prefix=domain_prefix,
    )
    size = _page_size(state, limit)
    rows, nxt = read.search(session, access.grant, filters, sort=sort, cursor=cursor, limit=size)
    items = read.build_stores(
        session,
        rows,
        profile=access.grant.profile,
        methodology_url=state.settings.api.methodology_url,
        evidence=state.evidence,
        min_confidence=min_confidence,
    )
    request.state.records = len(items)
    return Page(
        items=items,
        next_cursor=nxt,
        as_of=state.clock.now(),
        methodology_url=state.settings.api.methodology_url,
    )


_ = profiles  # schema registry import keeps profile ↔ schema mapping in one place
