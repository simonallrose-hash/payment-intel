"""Export jobs end to end (FR-EX-01…06, LR-17).

request → (awaiting_approval when the estimate exceeds `export_max_rows`,
FR-EX-06) → run: rows within the segment + canaries, watermark order, file
in S3 with a `.schema.json` sidecar, usage row with the record count
(counts against the quota, FR-EX-03) → signed 72-hour link, each download
journaled (FR-EX-04).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from payintel.api.usage import log_usage
from payintel.core import audit
from payintel.core.clock import Clock
from payintel.core.errors import ConflictError, NotFoundError, ValidationError
from payintel.core.models.base import ExportFormat, ExportStatus, ExportType, OrgStatus, Role
from payintel.core.models.exports import Canary, ExportJob
from payintel.core.models.orgs import Contract, Entitlement, Organization
from payintel.core.s3 import ObjectStore
from payintel.core.settings import Settings
from payintel.entitlements.check import require_role, require_same_org, require_staff
from payintel.entitlements.model import Grant, Principal
from payintel.entitlements.quotas import check_record_quota
from payintel.entitlements.segment import countries_in_segment, platforms_in_segment
from payintel.exports import builder, canary, watermark
from payintel.exports.csv import write_csv
from payintel.exports.parquet import write_parquet
from payintel.exports.schema import dictionary, fields_for


@dataclass(frozen=True)
class ExportSpec:
    type: ExportType
    format: ExportFormat
    since: date | None = None
    countries: tuple[str, ...] = ()
    platforms: tuple[str, ...] = ()

    def params(self) -> dict[str, Any]:
        return {
            "since": self.since.isoformat() if self.since else None,
            "countries": list(self.countries),
            "platforms": list(self.platforms),
        }


def estimate_rows(session: Session, grant: Grant, spec: ExportSpec) -> int:
    from sqlalchemy import select as sa_select

    from payintel.core.models.domains import Domain, Host
    from payintel.core.models.store import ChangeEvent, StoreProfile
    from payintel.entitlements.lineage_guard import c1_visible_clause
    from payintel.entitlements.segment import segment_clause

    cs = countries_in_segment(grant, list(spec.countries))
    ps = platforms_in_segment(grant, list(spec.platforms))
    base = (
        sa_select(func.count(StoreProfile.host_id))
        .join(Host, Host.id == StoreProfile.host_id)
        .join(Domain, Domain.id == Host.domain_id)
        .where(Host.is_primary.is_(True), c1_visible_clause(), segment_clause(grant))
    )
    if cs:
        base = base.where(StoreProfile.country.in_(cs))
    if ps:
        base = base.where(StoreProfile.platform_id.in_(ps))
    if spec.type == ExportType.MARKET_AGGREGATES:
        return int(session.execute(base).scalar_one())  # upper bound of cells
    if spec.type == ExportType.INCREMENT:
        q = (
            sa_select(func.count(ChangeEvent.id))
            .join(StoreProfile, StoreProfile.host_id == ChangeEvent.host_id)
            .join(Host, Host.id == ChangeEvent.host_id)
            .join(Domain, Domain.id == Host.domain_id)
            .where(ChangeEvent.suppressed.is_(False), c1_visible_clause(), segment_clause(grant))
        )
        if spec.since:
            q = q.where(
                ChangeEvent.detected_at
                >= datetime.combine(spec.since, datetime.min.time(), tzinfo=UTC)
            )
        if cs:
            q = q.where(StoreProfile.country.in_(cs))
        if ps:
            q = q.where(StoreProfile.platform_id.in_(ps))
        return int(session.execute(q).scalar_one())
    return int(session.execute(base).scalar_one())


def request_export(
    session: Session,
    *,
    grant: Grant,
    principal: Principal,
    spec: ExportSpec,
    settings: Settings,
    clock: Clock,
) -> ExportJob:
    require_role(principal, Role.ORG_ANALYST)
    if spec.type == ExportType.INCREMENT and spec.since is None:
        raise ValidationError("`since` is required for increment exports")
    now = clock.now()
    estimate = estimate_rows(session, grant, spec)
    status = ExportStatus.PENDING
    if estimate > grant.export_max_rows:
        status = ExportStatus.AWAITING_APPROVAL
    job = ExportJob(
        org_id=grant.org_id,
        type=spec.type,
        format=spec.format,
        params=spec.params() | {"estimate": estimate, "profile": grant.profile.value},
        status=status,
        requested_by=principal.user_id or principal.api_key_id,
        created_at=now,
    )
    session.add(job)
    session.flush()
    audit.record(
        session,
        actor=principal.actor,
        action="export.request",
        object_type="export_job",
        object_id=str(job.id),
        after={
            "type": spec.type.value,
            "format": spec.format.value,
            "status": status.value,
            "estimate": estimate,
        },
        ip=principal.ip,
        clock=clock,
    )
    return job


def get_job(session: Session, principal: Principal, job_id: uuid.UUID) -> ExportJob:
    job = session.get(ExportJob, job_id)
    if job is None:
        raise NotFoundError("export not found", id=str(job_id))
    require_same_org(principal, job.org_id)
    return job


def approve(session: Session, job: ExportJob, *, principal: Principal, clock: Clock) -> ExportJob:
    """FR-EX-06: only `staff_compliance` (or `staff_admin`) may release a large export."""
    require_staff(principal, Role.STAFF_COMPLIANCE, Role.STAFF_ADMIN)
    if job.status != ExportStatus.AWAITING_APPROVAL:
        raise ConflictError("export is not awaiting approval", status=job.status.value)
    job.status = ExportStatus.PENDING
    job.approved_by = principal.user_id
    session.flush()
    audit.record(
        session,
        actor=principal.actor,
        action="export.approve",
        object_type="export_job",
        object_id=str(job.id),
        ip=principal.ip,
        clock=clock,
    )
    return job


def _row_key(export_type: ExportType) -> Any:
    if export_type == ExportType.FULL_SNAPSHOT:
        return lambda r: str(r["domain"])
    if export_type == ExportType.INCREMENT:
        return lambda r: f"{r['domain']}|{r['event_type']}|{r['entity']}|{r['detected_at']}"
    return lambda r: f"{r['country']}|{r['platform_id']}|{r['provider_id']}"


def file_key(job: ExportJob) -> str:
    ext = "parquet" if job.format == ExportFormat.PARQUET else "csv"
    return f"exports/{job.org_id}/{job.id}.{ext}"


def run_job(
    session: Session,
    job: ExportJob,
    *,
    grant: Grant,
    store: ObjectStore,
    settings: Settings,
    clock: Clock,
) -> ExportJob:
    """Build, watermark, upload. Raises on quota; marks the job failed on errors."""
    if job.status != ExportStatus.PENDING:
        raise ConflictError("export is not pending", status=job.status.value)
    now = clock.now()
    job.status = ExportStatus.RUNNING
    session.flush()
    spec = ExportSpec(
        job.type,
        job.format,
        date.fromisoformat(job.params["since"]) if job.params.get("since") else None,
        tuple(job.params.get("countries", [])),
        tuple(job.params.get("platforms", [])),
    )
    try:
        countries = list(spec.countries)
        platforms = list(spec.platforms)
        if job.type == ExportType.FULL_SNAPSHOT:
            rows = builder.snapshot_rows(session, grant, countries=countries, platforms=platforms)
            seg_countries = countries or sorted(grant.countries)
            seg_platforms = platforms or sorted(grant.platforms)
            rows += canary.canary_rows(
                session,
                org_id=job.org_id,
                export_id=job.id,
                zone=settings.identity.canary_zone,
                countries=seg_countries,
                platforms=seg_platforms,
                now=now,
                low=settings.export.canary_min,
                high=settings.export.canary_max,
            )
        elif job.type == ExportType.INCREMENT:
            rows = builder.increment_rows(
                session,
                grant,
                since=spec.since,
                until=now,
                countries=countries,
                platforms=platforms,
            )
            rows += [
                {
                    "domain": c["domain"],
                    "event_type": "provider_added",
                    "entity": c["provider_ids"][0],
                    "old_value": None,
                    "new_value": c["provider_ids"][0],
                    "detected_at": c["as_of"],
                }
                for c in canary.canary_rows(
                    session,
                    org_id=job.org_id,
                    export_id=job.id,
                    zone=settings.identity.canary_zone,
                    countries=countries or sorted(grant.countries),
                    platforms=platforms or sorted(grant.platforms),
                    now=now,
                    low=settings.export.canary_min,
                    high=settings.export.canary_max,
                )
            ]
        else:
            rows = builder.aggregate_rows(
                session,
                grant,
                countries=countries,
                platforms=platforms,
                min_cell=settings.quality.report_min_cell_size,
            )
        real_rows = len(rows) - len(
            [r for r in rows if str(r.get("domain", "")).endswith(settings.identity.canary_zone)]
        )
        check_record_quota(
            session,
            job.org_id,
            daily_limit=grant.daily_records,
            monthly_limit=grant.monthly_records,
            now=now,
            about_to_add=real_rows,
        )
        if real_rows > grant.export_max_rows and job.approved_by is None:
            raise ConflictError("export exceeds the row limit and is not approved")
        rows = watermark.order_rows(rows, job.watermark_id, _row_key(job.type))
        fields = fields_for(job.type.value, grant.profile)
        meta = watermark.metadata(
            export_id=job.id,
            watermark_id=job.watermark_id,
            org_id=job.org_id,
            export_type=job.type.value,
            profile=grant.profile.value,
            generated_at=now.isoformat(),
            methodology_url=settings.api.methodology_url,
        )
        data = (
            write_parquet(rows, fields, meta)
            if job.format == ExportFormat.PARQUET
            else write_csv(rows, fields, meta)
        )
        key = file_key(job)
        bucket = settings.s3.bucket_exports
        store.ensure_bucket(bucket)
        content_type = (
            "application/vnd.apache.parquet" if job.format == ExportFormat.PARQUET else "text/csv"
        )
        store.put_bytes(bucket, key, data, content_type=content_type)
        store.put_bytes(
            bucket,
            key + ".schema.json",
            json.dumps(dictionary(fields, meta), indent=2).encode(),
            content_type="application/json",
        )
        job.rows = len(rows)
        job.file_key = key
        job.status = ExportStatus.DONE
        job.finished_at = now
        job.expires_at = now + timedelta(hours=settings.retention.export_link_hours)
        job.canary_ids = [
            c.id
            for c in session.execute(
                select(Canary).where(Canary.export_job_id == job.id).order_by(Canary.id)
            ).scalars()
        ]
        log_usage(
            session,
            org_id=job.org_id,
            endpoint="exports.build",
            params={"export_id": str(job.id), "type": job.type.value},
            records=real_rows,
            ip=None,
            ts=now,
            duration_ms=int((clock.now() - now).total_seconds() * 1000),
            user_id=job.requested_by,
        )
        session.flush()
    except Exception as exc:
        job.status = ExportStatus.FAILED
        job.finished_at = now
        job.params = dict(job.params) | {"error": str(exc)[:500]}
        session.flush()
        raise
    return job


def download(
    session: Session,
    job: ExportJob,
    *,
    principal: Principal,
    store: ObjectStore,
    settings: Settings,
    clock: Clock,
) -> str:
    """Signed URL valid until `expires_at`; every call is journaled (FR-EX-04)."""
    require_same_org(principal, job.org_id)
    now = clock.now()
    if job.status != ExportStatus.DONE or not job.file_key:
        raise ConflictError("export is not ready", status=job.status.value)
    if job.expires_at is not None and job.expires_at <= now:
        job.status = ExportStatus.EXPIRED
        session.flush()
        raise ConflictError("export link expired", status="expired")
    remaining = int((job.expires_at - now).total_seconds()) if job.expires_at else 3600
    url = store.presigned_get_url(
        settings.s3.bucket_exports, job.file_key, expires_seconds=max(60, remaining)
    )
    job.download_count += 1
    log_usage(
        session,
        org_id=job.org_id,
        endpoint="exports.download",
        params={"export_id": str(job.id)},
        records=0,
        ip=principal.ip,
        ts=now,
        duration_ms=0,
        api_key_id=principal.api_key_id,
        user_id=principal.user_id,
    )
    audit.record(
        session,
        actor=principal.actor,
        action="export.download",
        object_type="export_job",
        object_id=str(job.id),
        ip=principal.ip,
        clock=clock,
    )
    return url


def list_jobs(session: Session, org_id: uuid.UUID, *, limit: int = 50) -> list[ExportJob]:
    return list(
        session.execute(
            select(ExportJob)
            .where(ExportJob.org_id == org_id)
            .order_by(ExportJob.created_at.desc())
            .limit(limit)
        ).scalars()
    )


def schedule_periodic(session: Session, *, settings: Settings, clock: Clock) -> list[ExportJob]:
    """FR-EX-03: one `full_snapshot` per calendar month for every active entitlement
    whose `export_schedule` is `monthly` (idempotent per month)."""
    now = clock.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    created: list[ExportJob] = []
    rows = session.execute(
        select(Organization, Entitlement)
        .join(Contract, Contract.org_id == Organization.id)
        .join(Entitlement, Entitlement.contract_id == Contract.id)
        .where(
            Organization.status == OrgStatus.ACTIVE,
            Entitlement.active.is_(True),
            Entitlement.export_schedule == "monthly",
            Contract.starts_on <= now.date(),
            Contract.ends_on >= now.date(),
        )
    ).all()
    for org, _ent in rows:
        exists = session.execute(
            select(ExportJob.id).where(
                ExportJob.org_id == org.id,
                ExportJob.type == ExportType.FULL_SNAPSHOT,
                ExportJob.created_at >= month_start,
                ExportJob.params["scheduled"].as_boolean().is_(True),
            )
        ).first()
        if exists:
            continue
        job = ExportJob(
            org_id=org.id,
            type=ExportType.FULL_SNAPSHOT,
            format=ExportFormat.PARQUET,
            params={"since": None, "countries": [], "platforms": [], "scheduled": True},
            status=ExportStatus.PENDING,
            created_at=now,
        )
        session.add(job)
        created.append(job)
    session.flush()
    return created


def pending_jobs(session: Session, *, limit: int = 10) -> list[ExportJob]:
    return list(
        session.execute(
            select(ExportJob)
            .where(ExportJob.status == ExportStatus.PENDING)
            .order_by(ExportJob.created_at)
            .limit(limit)
        ).scalars()
    )
