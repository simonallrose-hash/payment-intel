"""`POST /v1/exports`, `GET /v1/exports/{id}` (FR-EX-03/04)."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from payintel.api.deps import Access, SessionDep, StateDep, scoped
from payintel.api.schemas.common import ExportIn, ExportOut
from payintel.core.errors import ConfigurationError
from payintel.core.models.base import ExportFormat, ExportStatus, ExportType
from payintel.core.models.exports import ExportJob
from payintel.exports import service

router = APIRouter(prefix="/v1/exports", tags=["exports"])
ReadAccess = Annotated[Access, Depends(scoped("stores:read"))]
WriteAccess = Annotated[Access, Depends(scoped("exports:write"))]


def _out(job: ExportJob, url: str | None) -> ExportOut:
    return ExportOut(
        id=str(job.id),
        type=job.type.value,
        format=job.format.value,
        status=job.status.value,
        rows=job.rows,
        download_url=url,
        expires_at=job.expires_at,
        created_at=job.created_at,
        finished_at=job.finished_at,
    )


@router.post("", response_model=ExportOut, status_code=202)
def request_export(
    body: ExportIn, request: Request, state: StateDep, session: SessionDep, access: WriteAccess
) -> Any:
    spec = service.ExportSpec(
        ExportType(body.type),
        ExportFormat(body.format),
        body.since,
        tuple(body.countries),
        tuple(body.platforms),
    )
    job = service.request_export(
        session,
        grant=access.grant,
        principal=access.principal,
        spec=spec,
        settings=state.settings,
        clock=state.clock,
    )
    request.state.records = 0
    return _out(job, None)


@router.get("", response_model=list[ExportOut])
def list_exports(request: Request, session: SessionDep, access: ReadAccess) -> Any:
    request.state.records = 0
    return [_out(j, None) for j in service.list_jobs(session, access.org_id)]


@router.get("/{export_id}", response_model=ExportOut)
def get_export(
    export_id: uuid.UUID, request: Request, state: StateDep, session: SessionDep, access: ReadAccess
) -> Any:
    job = service.get_job(session, access.principal, export_id)
    url: str | None = None
    if job.status == ExportStatus.DONE:
        if state.store is None:
            raise ConfigurationError("object store is not configured")
        url = service.download(
            session,
            job,
            principal=access.principal,
            store=state.store,
            settings=state.settings,
            clock=state.clock,
        )
    request.state.records = 0
    return _out(job, url)
