"""CRUD `/v1/watchlists` (FR-AL-01)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from payintel.alerts import watchlists as wl
from payintel.api.deps import Access, SessionDep, StateDep, scoped
from payintel.api.schemas.common import (
    WatchlistIn,
    WatchlistItemsIn,
    WatchlistItemsOut,
    WatchlistOut,
)
from payintel.core.models.alerts import Watchlist, WatchlistItem

router = APIRouter(prefix="/v1/watchlists", tags=["watchlists"])
ReadAccess = Annotated[Access, Depends(scoped("stores:read"))]
WriteAccess = Annotated[Access, Depends(scoped("watchlists:write"))]


def _out(session: Any, w: Watchlist) -> WatchlistOut:
    return WatchlistOut(
        id=w.id, name=w.name, items=wl.item_count(session, w.id), created_at=w.created_at
    )


@router.get("", response_model=list[WatchlistOut])
def list_watchlists(request: Request, session: SessionDep, access: ReadAccess) -> Any:
    rows = session.execute(
        select(Watchlist).where(Watchlist.org_id == access.org_id).order_by(Watchlist.id)
    ).scalars()
    request.state.records = 0
    return [_out(session, w) for w in rows]


@router.post("", response_model=WatchlistOut, status_code=201)
def create_watchlist(
    body: WatchlistIn, request: Request, state: StateDep, session: SessionDep, access: WriteAccess
) -> Any:
    w = wl.create_watchlist(
        session,
        org_id=access.org_id,
        name=body.name,
        actor=access.principal.actor,
        clock=state.clock,
    )
    if body.domains:
        wl.add_domains(
            session,
            w,
            body.domains,
            limit=access.grant.watchlist_limit,
            actor=access.principal.actor,
            clock=state.clock,
        )
    request.state.records = 0
    return _out(session, w)


@router.get("/{watchlist_id}", response_model=WatchlistOut)
def get_watchlist(
    watchlist_id: int, request: Request, session: SessionDep, access: ReadAccess
) -> Any:
    request.state.records = 0
    return _out(session, wl.get_watchlist(session, access.org_id, watchlist_id))


@router.get("/{watchlist_id}/domains", response_model=list[str])
def list_domains(
    watchlist_id: int, request: Request, session: SessionDep, access: ReadAccess
) -> Any:
    w = wl.get_watchlist(session, access.org_id, watchlist_id)
    rows = session.execute(
        select(WatchlistItem.domain)
        .where(WatchlistItem.watchlist_id == w.id)
        .order_by(WatchlistItem.domain)
    ).scalars()
    request.state.records = 0
    return list(rows)


@router.post("/{watchlist_id}/domains", response_model=WatchlistItemsOut)
def add_domains(
    watchlist_id: int,
    body: WatchlistItemsIn,
    request: Request,
    state: StateDep,
    session: SessionDep,
    access: WriteAccess,
) -> Any:
    w = wl.get_watchlist(session, access.org_id, watchlist_id)
    r = wl.add_domains(
        session,
        w,
        body.domains,
        limit=access.grant.watchlist_limit,
        actor=access.principal.actor,
        clock=state.clock,
    )
    request.state.records = 0
    return WatchlistItemsOut(added=r.added, skipped=r.skipped, total=r.total)


@router.delete("/{watchlist_id}/domains/{domain}", status_code=204)
def remove_domain(
    watchlist_id: int, domain: str, request: Request, session: SessionDep, access: WriteAccess
) -> None:
    w = wl.get_watchlist(session, access.org_id, watchlist_id)
    wl.remove_domain(session, w, domain)
    request.state.records = 0


@router.delete("/{watchlist_id}", status_code=204)
def delete_watchlist(
    watchlist_id: int, request: Request, state: StateDep, session: SessionDep, access: WriteAccess
) -> None:
    w = wl.get_watchlist(session, access.org_id, watchlist_id)
    wl.delete_watchlist(session, w, actor=access.principal.actor, clock=state.clock)
    request.state.records = 0
