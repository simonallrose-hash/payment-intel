"""Report jobs for `staff_analyst` (FR-RP-01, AC-15): build, store in S3, journal."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.core import audit
from payintel.core.clock import Clock
from payintel.core.errors import NotFoundError
from payintel.core.models.base import ReportStatus, Role
from payintel.core.models.portal import ReportJob
from payintel.core.s3 import ObjectStore
from payintel.core.settings import Settings
from payintel.entitlements.check import require_staff
from payintel.entitlements.model import Principal
from payintel.reports import aggregates
from payintel.reports.csv import write_csv_zip
from payintel.reports.xlsx import write_xlsx


def build_report(
    session: Session,
    spec: aggregates.ReportSpec,
    *,
    principal: Principal,
    store: ObjectStore | None,
    settings: Settings,
    clock: Clock,
    ch: Any = None,
    org_id: uuid.UUID | None = None,
) -> tuple[ReportJob, bytes, bytes]:
    """Synchronous build (seconds for one country); returns the job and both files."""
    require_staff(principal, Role.STAFF_ANALYST, Role.STAFF_ADMIN)
    now = clock.now()
    job = ReportJob(
        spec=spec.as_dict(), requested_by=principal.actor, created_at=now, org_id=org_id
    )
    session.add(job)
    session.flush()
    try:
        report = aggregates.build(session, spec, now=now, ch=ch)
        xlsx = write_xlsx(report, methodology_url=settings.api.methodology_url)
        csv_zip = write_csv_zip(report, methodology_url=settings.api.methodology_url)
        if store is not None:
            bucket = settings.s3.bucket_exports
            store.ensure_bucket(bucket)
            job.xlsx_key = f"reports/{job.id}.xlsx"
            job.csv_key = f"reports/{job.id}.csv.zip"
            store.put_bytes(
                bucket,
                job.xlsx_key,
                xlsx,
                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            store.put_bytes(bucket, job.csv_key, csv_zip, content_type="application/zip")
        job.summary = {
            "total_stores": report.total_stores,
            "cells": len(report.cells),
            "cells_withheld": report.suppressed_cells,
            "min_published": report.min_published(),
            "rows": {
                "psp_share": len(report.psp_share),
                "method_share": len(report.method_share),
                "bnpl_share": len(report.bnpl_share),
                "psp_flows": len(report.psp_flows),
                "monthly": len(report.monthly),
            },
        }
        job.status = ReportStatus.DONE
        job.finished_at = clock.now()
    except Exception as exc:
        job.status = ReportStatus.FAILED
        job.error = str(exc)[:500]
        job.finished_at = clock.now()
        session.flush()
        raise
    session.flush()
    audit.record(
        session,
        actor=principal.actor,
        action="report.build",
        object_type="report_job",
        object_id=str(job.id),
        after=job.summary | {"spec": job.spec},
        ip=principal.ip,
        clock=clock,
    )
    return job, xlsx, csv_zip


def get_job(session: Session, job_id: uuid.UUID) -> ReportJob:
    job = session.get(ReportJob, job_id)
    if job is None:
        raise NotFoundError("report not found", id=str(job_id))
    return job


def recent(
    session: Session, *, limit: int = 30, org_id: uuid.UUID | None = None
) -> list[ReportJob]:
    q = select(ReportJob).order_by(ReportJob.created_at.desc()).limit(limit)
    if org_id is not None:
        q = q.where(ReportJob.org_id == org_id)
    return list(session.execute(q).scalars())
