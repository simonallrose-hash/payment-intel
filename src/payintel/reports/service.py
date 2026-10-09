"""Report jobs for `staff_analyst` (FR-RP-01, AC-15): build, store in S3, journal."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.core import audit
from payintel.core.clock import Clock
from payintel.core.errors import ConflictError, NotFoundError
from payintel.core.models.base import ReportStatus, Role
from payintel.core.models.portal import ReportJob
from payintel.core.s3 import ObjectStore
from payintel.core.settings import Settings
from payintel.entitlements.check import require_staff
from payintel.entitlements.model import Principal
from payintel.reports import aggregates, public
from payintel.reports.csv import write_csv_zip
from payintel.reports.pdf import write_pdf, write_public_pdf
from payintel.reports.xlsx import write_xlsx

MEDIA = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "application/zip",
    "pdf": "application/pdf",
}
EXT = {"xlsx": "xlsx", "csv": "csv.zip", "pdf": "pdf"}


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
        url = settings.api.methodology_url
        xlsx = write_xlsx(report, methodology_url=url)
        csv_zip = write_csv_zip(report, methodology_url=url)
        pdf = write_pdf(report, methodology_url=url)
        job.public_summary = public.from_report(report).as_dict()
        if store is not None:
            bucket = settings.s3.bucket_exports
            store.ensure_bucket(bucket)
            job.xlsx_key = f"reports/{job.id}.xlsx"
            job.csv_key = f"reports/{job.id}.csv.zip"
            job.pdf_key = f"reports/{job.id}.pdf"
            store.put_bytes(bucket, job.xlsx_key, xlsx, content_type=MEDIA["xlsx"])
            store.put_bytes(bucket, job.csv_key, csv_zip, content_type=MEDIA["csv"])
            store.put_bytes(bucket, job.pdf_key, pdf, content_type=MEDIA["pdf"])
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


def file_for(job: ReportJob, kind: str) -> tuple[str, str, str]:
    """(object key, media type, download file name) for xlsx | csv | pdf."""
    key = {"xlsx": job.xlsx_key, "csv": job.csv_key, "pdf": job.pdf_key}.get(kind)
    if not key:
        raise NotFoundError("file not available", kind=kind)
    return key, MEDIA[kind], f"payintel-report-{job.id}.{EXT[kind]}"


def _slug(job: ReportJob) -> str:
    countries = "-".join(c.lower() for c in job.spec.get("countries", [])) or "all"
    return f"{countries}-{job.created_at:%Y-%m}-{job.id.hex[:8]}"


def publish(
    session: Session,
    job: ReportJob,
    *,
    principal: Principal,
    store: ObjectStore | None,
    settings: Settings,
    clock: Clock,
) -> ReportJob:
    """FR-RP-05: expose the summary (no domains) on the public pages and as a PDF."""
    require_staff(principal, Role.STAFF_ANALYST, Role.STAFF_ADMIN)
    if job.status != ReportStatus.DONE or not job.public_summary:
        raise ConflictError("only a finished report can be published")
    summary = public.PublicSummary.from_dict(job.public_summary)
    if store is not None:
        job.public_pdf_key = f"reports/{job.id}-public.pdf"
        store.put_bytes(
            settings.s3.bucket_exports,
            job.public_pdf_key,
            write_public_pdf(summary, methodology_url=settings.api.methodology_url),
            content_type=MEDIA["pdf"],
        )
    job.public_slug = job.public_slug or _slug(job)
    job.published_at = clock.now()
    job.published_by = principal.actor
    session.flush()
    audit.record(
        session,
        actor=principal.actor,
        action="report.publish",
        object_type="report_job",
        object_id=str(job.id),
        after={"slug": job.public_slug, "cells": len(summary.cells)},
        ip=principal.ip,
        clock=clock,
    )
    return job


def unpublish(session: Session, job: ReportJob, *, principal: Principal, clock: Clock) -> ReportJob:
    require_staff(principal, Role.STAFF_ANALYST, Role.STAFF_ADMIN)
    if job.published_at is None:
        raise ConflictError("report is not published")
    before = {"slug": job.public_slug}
    job.public_slug = None
    job.published_at = None
    job.published_by = None
    session.flush()
    audit.record(
        session,
        actor=principal.actor,
        action="report.unpublish",
        object_type="report_job",
        object_id=str(job.id),
        before=before,
        ip=principal.ip,
        clock=clock,
    )
    return job


def published(session: Session, *, limit: int = 50) -> list[ReportJob]:
    return list(
        session.execute(
            select(ReportJob)
            .where(ReportJob.published_at.is_not(None))
            .order_by(ReportJob.published_at.desc())
            .limit(limit)
        ).scalars()
    )


def by_slug(session: Session, slug: str) -> ReportJob:
    job = session.execute(
        select(ReportJob).where(ReportJob.public_slug == slug, ReportJob.published_at.is_not(None))
    ).scalar_one_or_none()
    if job is None:
        raise NotFoundError("report not found", slug=slug)
    return job
