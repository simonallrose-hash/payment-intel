"""`GET /v1/stats/market-share` (FR-API-03) with cell suppression (FR-RP-03, LR-19)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request

from payintel.api import read
from payintel.api.deps import Access, SessionDep, StateDep, scoped
from payintel.api.schemas.common import MarketShareCell, MarketShareOut
from payintel.exports.builder import cell_totals
from payintel.reports.wilson import wilson

router = APIRouter(prefix="/v1/stats", tags=["stats"])
StatsAccess = Annotated[Access, Depends(scoped("stats:read"))]


@router.get("/market-share", response_model=MarketShareOut)
def market_share(
    request: Request,
    state: StateDep,
    session: SessionDep,
    access: StatsAccess,
    country: Annotated[list[str], Query()] = [],  # noqa: B006
    platform: Annotated[list[str], Query()] = [],  # noqa: B006
    role: str | None = None,
) -> Any:
    min_cell = state.settings.quality.report_min_cell_size
    total, cells = read.market_share(
        session, access.grant, countries=country, platforms=platform, role=role, min_cell=min_cell
    )
    totals = cell_totals(session, access.grant, countries=country, platforms=platform)
    out: list[MarketShareCell] = []
    for c, p, provider, n in cells:
        t = totals.get((c, p), 0)
        lo, hi = wilson(n, t)
        out.append(
            MarketShareCell(
                country=c,
                platform_id=p,
                provider_id=provider,
                stores=n,
                share=(n / t) if t else 0.0,
                ci_low=lo,
                ci_high=hi,
            )
        )
    request.state.records = len(out)
    return MarketShareOut(
        as_of=state.clock.now(),
        total_stores=total,
        min_cell_size=min_cell,
        cells=out,
        methodology_url=state.settings.api.methodology_url,
    )
