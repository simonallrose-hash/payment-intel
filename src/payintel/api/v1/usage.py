"""`GET /v1/usage`: quota consumption of the caller's organisation (FR-API-09)."""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select

from payintel.api.deps import Access, SessionDep, StateDep, scoped
from payintel.api.schemas.common import UsageOut
from payintel.core.models.audit import UsageLog
from payintel.entitlements.quotas import _day_start, _month_start, records_used

router = APIRouter(prefix="/v1/usage", tags=["usage"])
UsageAccess = Annotated[Access, Depends(scoped("usage:read"))]


@router.get("", response_model=UsageOut)
def usage(request: Request, state: StateDep, session: SessionDep, access: UsageAccess) -> Any:
    now = state.clock.now()
    org = access.org_id
    requests_24h = int(
        session.execute(
            select(func.count(UsageLog.id)).where(
                UsageLog.org_id == org, UsageLog.ts >= now - timedelta(hours=24)
            )
        ).scalar_one()
    )
    request.state.records = 0
    return UsageOut(
        org_id=str(org),
        day_records=records_used(session, org, since=_day_start(now)),
        month_records=records_used(session, org, since=_month_start(now)),
        daily_limit=access.grant.daily_records,
        monthly_limit=access.grant.monthly_records,
        api_rps=access.grant.api_rps,
        requests_last_24h=requests_24h,
        as_of=now,
    )
