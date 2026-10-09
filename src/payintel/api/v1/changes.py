"""`GET /v1/changes`: event feed over the segment (FR-API-03)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request

from payintel.api import read
from payintel.api.deps import Access, SessionDep, StateDep, scoped
from payintel.api.schemas.c1_basic import ChangeBasic
from payintel.api.schemas.c1_full import ChangeFull
from payintel.api.schemas.common import Page
from payintel.core.errors import ValidationError

router = APIRouter(prefix="/v1/changes", tags=["changes"])
ChangesAccess = Annotated[Access, Depends(scoped("changes:read"))]


@router.get("", response_model=Page[ChangeBasic | ChangeFull])
def list_changes(
    request: Request,
    state: StateDep,
    session: SessionDep,
    access: ChangesAccess,
    since: datetime | None = None,
    until: datetime | None = None,
    event_type: Annotated[list[str], Query()] = [],  # noqa: B006
    country: Annotated[list[str], Query()] = [],  # noqa: B006
    platform: Annotated[list[str], Query()] = [],  # noqa: B006
    cursor: str | None = None,
    limit: int | None = None,
) -> Any:
    maximum = state.settings.api.max_page_size
    size = limit if limit is not None else state.settings.api.history_page_size
    if size < 1 or size > maximum:
        raise ValidationError(f"limit must be between 1 and {maximum}", limit=size)
    items, nxt = read.changes(
        session,
        access.grant,
        profile=access.grant.profile,
        since=since,
        until=until,
        event_types=event_type,
        countries=country,
        platforms=platform,
        limit=size,
        cursor=cursor,
    )
    request.state.records = len(items)
    return Page(
        items=items,
        next_cursor=nxt,
        as_of=state.clock.now(),
        methodology_url=state.settings.api.methodology_url,
    )
