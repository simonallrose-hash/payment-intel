"""`payintel exports run`: build pending export jobs and schedule periodic ones (FR-EX-03)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from payintel.core.clock import Clock
from payintel.core.flags import FlagService
from payintel.core.logging import get_logger
from payintel.core.models.base import ExportStatus
from payintel.core.models.exports import ExportJob
from payintel.core.s3 import ObjectStore
from payintel.core.settings import Settings
from payintel.entitlements.check import EntitlementDenied, resolve_grant
from payintel.exports import service

log = get_logger(__name__)


@dataclass
class RunResult:
    scheduled: int = 0
    built: int = 0
    failed: list[str] = field(default_factory=list)


def run_once(
    session_factory: Callable[[], Session],
    *,
    settings: Settings,
    clock: Clock,
    store: ObjectStore,
    limit: int = 10,
) -> RunResult:
    """Schedule due periodic exports, then build up to `limit` pending jobs,
    each in its own transaction so one failure does not block the rest."""
    result = RunResult()
    session = session_factory()
    try:
        result.scheduled = len(service.schedule_periodic(session, settings=settings, clock=clock))
        session.commit()
        job_ids = [j.id for j in service.pending_jobs(session, limit=limit)]
    finally:
        session.close()
    for job_id in job_ids:
        session = session_factory()
        try:
            job = session.get(ExportJob, job_id)
            if job is None or job.status != ExportStatus.PENDING:
                continue
            flags = FlagService(session, settings.flags, clock=clock)
            grant = resolve_grant(session, job.org_id, today=clock.now().date(), flags=flags)
            service.run_job(session, job, grant=grant, store=store, settings=settings, clock=clock)
            session.commit()
            result.built += 1
        except EntitlementDenied as exc:
            session.rollback()
            _mark_failed(session_factory, job_id, f"entitlement: {exc.reason}", clock)
            result.failed.append(str(job_id))
        except Exception as exc:
            session.rollback()
            log.exception("export job failed", job_id=str(job_id))
            _mark_failed(session_factory, job_id, str(exc)[:300], clock)
            result.failed.append(str(job_id))
        finally:
            session.close()
    return result


def _mark_failed(
    session_factory: Callable[[], Session], job_id: object, reason: str, clock: Clock
) -> None:
    session = session_factory()
    try:
        job = session.get(ExportJob, job_id)
        if job is not None and job.status in (ExportStatus.PENDING, ExportStatus.RUNNING):
            job.status = ExportStatus.FAILED
            job.finished_at = clock.now()
            job.params = {**job.params, "error": reason}
            session.commit()
    finally:
        session.close()
