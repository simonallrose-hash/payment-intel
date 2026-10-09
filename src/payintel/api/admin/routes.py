"""Staff admin (FR-ADM-01…05, FR-KYC-*, FR-QA-03/06, FR-RP-01, FR-OO-02/03, FR-EX-06, FR-AB-01).

Every page checks the staff role through `require_staff` (3.2 separation of
duties); every change goes through a compliance/service function that writes
the audit log. Read-only pages are open to all staff roles; internal domain
search (AS-23 fields) is `staff_analyst`/`staff_admin` only (FR-ADM-04).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.responses import HTMLResponse, Response

from payintel.abuse import canary as canary_mod
from payintel.abuse import incidents as incidents_mod
from payintel.abuse import usage_report as usage_mod
from payintel.api import ui
from payintel.api.deps import SessionDep, StateDep
from payintel.api.schemas.internal import StoreInternal
from payintel.compliance import contracts as contracts_mod
from payintel.compliance import dsar as dsar_mod
from payintel.compliance import kyc as kyc_mod
from payintel.compliance import lifecycle
from payintel.compliance import optout as optout_mod
from payintel.compliance import users as users_mod
from payintel.core import audit
from payintel.core.errors import ConfigurationError, NotFoundError, ValidationError
from payintel.core.flags import FlagService
from payintel.core.models.abuse import AbuseIncident
from payintel.core.models.access import ApiKey
from payintel.core.models.audit import AuditLog
from payintel.core.models.base import (
    DomainSourceKind,
    DsarStatus,
    ExportStatus,
    FieldProfile,
    KycDecision,
    OrgStatus,
    PageScope,
    Product,
    Role,
    RuleTargetType,
    ScanStatus,
    ScanType,
    SignalType,
)
from payintel.core.models.domains import Domain, DomainSource, Host
from payintel.core.models.exports import ExportJob
from payintel.core.models.orgs import KycRecord, Organization
from payintel.core.models.quality import QualityAlert
from payintel.core.models.scans import ScanPlan, ScanRun
from payintel.core.models.store import StoreCheckoutHost
from payintel.detect import admin as rule_admin
from payintel.detect.rules import MATCH_KINDS
from payintel.entitlements.check import require_staff
from payintel.entitlements.model import STAFF_ROLES
from payintel.exports import service as exports
from payintel.history.read import read_store
from payintel.quality import anomalies as anomalies_mod
from payintel.quality import dashboard as dash_mod
from payintel.quality import review as review_mod
from payintel.quality import stops as stops_mod
from payintel.quality.gold import gold_size
from payintel.reports import aggregates
from payintel.reports import service as reports

router = APIRouter(prefix="/admin", tags=["admin"], include_in_schema=False)

Compliance = Annotated[
    ui.UiContext, Depends(ui.staff_roles(Role.STAFF_COMPLIANCE, Role.STAFF_ADMIN))
]
Analyst = Annotated[ui.UiContext, Depends(ui.staff_roles(Role.STAFF_ANALYST, Role.STAFF_ADMIN))]
Admin = Annotated[ui.UiContext, Depends(ui.staff_roles(Role.STAFF_ADMIN))]


def _org(session: Session, org_id: uuid.UUID) -> Organization:
    org = session.get(Organization, org_id)
    if org is None:
        raise NotFoundError("organisation not found")
    return org


# --- overview -------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
def overview(request: Request, state: StateDep, session: SessionDep, ctx: ui.StaffDep) -> Any:
    by_status: dict[OrgStatus, int] = {
        status: int(n)
        for status, n in session.execute(
            select(Organization.status, func.count(Organization.id)).group_by(Organization.status)
        ).all()
    }
    counts = {
        "orgs": sum(by_status.values()),
        "by_status": {k.value: v for k, v in by_status.items()},
        "exports_awaiting": session.execute(
            select(func.count(ExportJob.id)).where(
                ExportJob.status == ExportStatus.AWAITING_APPROVAL
            )
        ).scalar_one(),
        "optout_pending": len(optout_mod.pending_requests(session)),
        "dsar_open": len(dsar_mod.open_requests(session)),
        "dsar_overdue": len(dsar_mod.overdue(session, clock=state.clock)),
        "gold": gold_size(session),
        "quality_alerts": session.execute(
            select(func.count(QualityAlert.id)).where(QualityAlert.acknowledged_at.is_(None))
        ).scalar_one(),
    }
    return ui.render(request, "admin/overview.html", ctx=ctx, counts=counts)


# --- organisations ------------------------------------------------------------------


@router.get("/orgs", response_class=HTMLResponse)
def orgs(request: Request, session: SessionDep, ctx: ui.StaffDep, q: str | None = None) -> Any:
    stmt = select(Organization).order_by(Organization.created_at.desc())
    if q:
        stmt = stmt.where(Organization.legal_name.ilike(f"%{q.strip()}%"))
    rows = list(session.execute(stmt.limit(200)).scalars())
    return ui.render(
        request, "admin/orgs.html", ctx=ctx, orgs=rows, q=q, statuses=[s.value for s in OrgStatus]
    )


@router.post("/orgs")
def org_create(
    state: StateDep,
    session: SessionDep,
    ctx: Compliance,
    _csrf: ui.CsrfDep,
    legal_name: Annotated[str, Form()],
    country: Annotated[str, Form()],
    reg_number: Annotated[str | None, Form()] = None,
) -> Response:
    if not legal_name.strip() or len(country.strip()) != 2:
        raise ValidationError("legal name and a 2-letter country are required")
    org = Organization(
        legal_name=legal_name.strip(),
        reg_number=(reg_number or "").strip() or None,
        country=country.strip().upper(),
        status=OrgStatus.APPLIED,
        created_at=state.clock.now(),
    )
    session.add(org)
    session.flush()
    audit.record(
        session,
        actor=ctx.principal.actor,
        action="org.create",
        object_type="organization",
        object_id=str(org.id),
        after={"legal_name": org.legal_name, "country": org.country},
        ip=ctx.principal.ip,
        clock=state.clock,
    )
    return ui.redirect(f"/admin/orgs/{org.id}", msg="Organisation created (status: applied).")


@router.get("/orgs/{org_id}", response_class=HTMLResponse)
def org_card(
    request: Request, state: StateDep, session: SessionDep, ctx: ui.StaffDep, org_id: uuid.UUID
) -> Any:
    org = _org(session, org_id)
    rec = session.execute(select(KycRecord).where(KycRecord.org_id == org.id)).scalar_one_or_none()
    keys = list(
        session.execute(
            select(ApiKey).where(ApiKey.org_id == org.id).order_by(ApiKey.created_at.desc())
        ).scalars()
    )
    flags = FlagService(session, state.settings.flags, clock=state.clock)
    return ui.render(
        request,
        "admin/org.html",
        ctx=ctx,
        org=org,
        dossier=kyc_mod.dossier_dict(rec),
        transitions=sorted(s.value for s in lifecycle.TRANSITIONS[org.status]),
        contracts=contracts_mod.contracts_of(session, org.id),
        members=users_mod.users_of_org(session, org.id),
        keys=keys,
        purposes=kyc_mod.PURPOSES,
        sanctions=kyc_mod.SANCTIONS_RESULTS,
        products=[p.value for p in Product],
        profiles=[p.value for p in FieldProfile],
        c2_enabled=flags.is_enabled("feature_c2_enabled"),
        org_roles=["org_viewer", "org_analyst", "org_admin"],
        incidents=incidents_mod.open_incidents(session, org.id),
        usage_reports=usage_mod.reports_of(session, org.id),
        audit_rows=list(
            session.execute(
                select(AuditLog)
                .where(AuditLog.object_id == str(org.id))
                .order_by(AuditLog.id.desc())
                .limit(30)
            ).scalars()
        ),
    )


@router.post("/orgs/{org_id}/status")
def org_status(
    state: StateDep,
    session: SessionDep,
    ctx: Compliance,
    _csrf: ui.CsrfDep,
    org_id: uuid.UUID,
    to: Annotated[str, Form()],
    comment: Annotated[str, Form()] = "",
) -> Response:
    org = _org(session, org_id)
    lifecycle.transition(
        session, org, OrgStatus(to), principal=ctx.principal, comment=comment, clock=state.clock
    )
    return ui.redirect(f"/admin/orgs/{org.id}", msg=f"Status: {org.status.value}.")


@router.post("/orgs/{org_id}/kyc")
def org_kyc(
    state: StateDep,
    session: SessionDep,
    ctx: Compliance,
    _csrf: ui.CsrfDep,
    org_id: uuid.UUID,
    address: Annotated[str | None, Form()] = None,
    website: Annotated[str | None, Form()] = None,
    contact_name: Annotated[str | None, Form()] = None,
    contact_title: Annotated[str | None, Form()] = None,
    purpose: Annotated[str | None, Form()] = None,
    beneficiaries: Annotated[str | None, Form()] = None,
) -> Response:
    org = _org(session, org_id)
    items: list[dict[str, Any]] = []
    for line in (beneficiaries or "").splitlines():
        if not line.strip():
            continue
        name, _, share = line.rpartition(";")
        if not name:
            raise ValidationError("beneficiary lines are `Name; share%`")
        items.append({"name": name.strip(), "share": float(share.strip().rstrip("%") or 0)})
    kyc_mod.update_dossier(
        session,
        org,
        kyc_mod.Dossier(
            address=(address or "").strip() or None,
            website=(website or "").strip() or None,
            beneficiaries=items,
            contact_name=(contact_name or "").strip() or None,
            contact_title=(contact_title or "").strip() or None,
            purpose=(purpose or "").strip() or None,
        ),
        principal=ctx.principal,
        clock=state.clock,
    )
    return ui.redirect(f"/admin/orgs/{org.id}", msg="Dossier saved.")


@router.post("/orgs/{org_id}/kyc/document")
async def org_kyc_document(
    state: StateDep,
    session: SessionDep,
    ctx: Compliance,
    _csrf: ui.CsrfDep,
    org_id: uuid.UUID,
    document: Annotated[UploadFile, File()],
) -> Response:
    org = _org(session, org_id)
    if state.store is None:
        raise ConfigurationError("object store is not configured")
    data = await document.read()
    if not data:
        raise ValidationError("empty file")
    safe = "".join(c for c in (document.filename or "document") if c.isalnum() or c in "._-")
    key = f"kyc/{org.id}/{uuid.uuid4().hex}-{safe}"
    state.store.put_bytes(
        state.settings.s3.bucket_artifacts,
        key,
        data,
        content_type=document.content_type or "application/octet-stream",
    )
    kyc_mod.add_document(session, org, key, principal=ctx.principal, clock=state.clock)
    return ui.redirect(f"/admin/orgs/{org.id}", msg="Document stored.")


@router.post("/orgs/{org_id}/kyc/sanctions")
def org_sanctions(
    state: StateDep,
    session: SessionDep,
    ctx: Compliance,
    _csrf: ui.CsrfDep,
    org_id: uuid.UUID,
    result: Annotated[str, Form()],
    source: Annotated[str, Form()],
    checked_at: Annotated[str | None, Form()] = None,
) -> Response:
    org = _org(session, org_id)
    when = (
        datetime.fromisoformat(checked_at).replace(tzinfo=UTC) if checked_at else state.clock.now()
    )
    kyc_mod.record_sanctions(
        session,
        org,
        result=result,
        source=source.strip(),
        checked_at=when,
        principal=ctx.principal,
        clock=state.clock,
    )
    return ui.redirect(f"/admin/orgs/{org.id}", msg="Sanctions check recorded.")


@router.post("/orgs/{org_id}/kyc/video")
def org_video(
    state: StateDep, session: SessionDep, ctx: Compliance, _csrf: ui.CsrfDep, org_id: uuid.UUID
) -> Response:
    org = _org(session, org_id)
    kyc_mod.record_video_call(
        session, org, at=state.clock.now(), principal=ctx.principal, clock=state.clock
    )
    return ui.redirect(f"/admin/orgs/{org.id}", msg="Video call recorded.")


@router.post("/orgs/{org_id}/kyc/decision")
def org_decision(
    state: StateDep,
    session: SessionDep,
    ctx: Compliance,
    _csrf: ui.CsrfDep,
    org_id: uuid.UUID,
    decision: Annotated[str, Form()],
    comment: Annotated[str, Form()] = "",
) -> Response:
    org = _org(session, org_id)
    kyc_mod.decide(
        session,
        org,
        KycDecision(decision),
        principal=ctx.principal,
        comment=comment,
        clock=state.clock,
    )
    return ui.redirect(f"/admin/orgs/{org.id}", msg=f"KYC decision: {decision}.")


@router.post("/orgs/{org_id}/contracts")
async def org_contract(
    state: StateDep,
    session: SessionDep,
    ctx: Compliance,
    _csrf: ui.CsrfDep,
    org_id: uuid.UUID,
    number: Annotated[str, Form()],
    product: Annotated[str, Form()],
    starts_on: Annotated[str, Form()],
    ends_on: Annotated[str, Form()],
    allowed_purposes: Annotated[str | None, Form()] = None,
    contract_file: Annotated[UploadFile | None, File()] = None,
) -> Response:
    org = _org(session, org_id)
    file_key: str | None = None
    if contract_file is not None and contract_file.filename:
        if state.store is None:
            raise ConfigurationError("object store is not configured")
        data = await contract_file.read()
        safe = "".join(c for c in contract_file.filename if c.isalnum() or c in "._-")
        file_key = f"contracts/{org.id}/{uuid.uuid4().hex}-{safe}"
        state.store.put_bytes(
            state.settings.s3.bucket_artifacts,
            file_key,
            data,
            content_type=contract_file.content_type or "application/octet-stream",
        )
    contracts_mod.create_contract(
        session,
        org,
        number=number.strip(),
        product=Product(product),
        starts_on=date.fromisoformat(starts_on),
        ends_on=date.fromisoformat(ends_on),
        allowed_purposes=ui.parse_list(allowed_purposes),
        file_key=file_key,
        principal=ctx.principal,
        clock=state.clock,
    )
    return ui.redirect(f"/admin/orgs/{org.id}", msg="Contract created.")


@router.post("/orgs/{org_id}/contracts/{contract_id}/entitlement")
def org_entitlement(
    state: StateDep,
    session: SessionDep,
    ctx: Compliance,
    _csrf: ui.CsrfDep,
    org_id: uuid.UUID,
    contract_id: uuid.UUID,
    field_profile: Annotated[str, Form()],
    countries: Annotated[str | None, Form()] = None,
    platforms: Annotated[str | None, Form()] = None,
    api_rps: Annotated[str | None, Form()] = None,
    daily_records: Annotated[str | None, Form()] = None,
    monthly_records: Annotated[str | None, Form()] = None,
    export_max_rows: Annotated[str | None, Form()] = None,
    export_schedule: Annotated[str | None, Form()] = None,
    watchlist_limit: Annotated[str | None, Form()] = None,
    allowed_ips: Annotated[str | None, Form()] = None,
) -> Response:
    org = _org(session, org_id)
    contract = contracts_mod.get_contract(session, contract_id)
    if contract.org_id != org.id:
        raise NotFoundError("contract not found")
    spec = contracts_mod.EntitlementSpec(
        field_profile=FieldProfile(field_profile),
        countries=[c.upper() for c in ui.parse_list(countries)],
        platforms=ui.parse_list(platforms),
        api_rps=ui.parse_int(api_rps),
        daily_records=ui.parse_int(daily_records),
        monthly_records=ui.parse_int(monthly_records),
        export_max_rows=ui.parse_int(export_max_rows),
        export_schedule=(export_schedule or "").strip() or None,
        watchlist_limit=ui.parse_int(watchlist_limit),
        allowed_ips=ui.parse_list(allowed_ips),
    )
    flags = FlagService(session, state.settings.flags, clock=state.clock)
    contracts_mod.set_entitlement(
        session,
        contract,
        spec,
        principal=ctx.principal,
        settings=state.settings,
        flags=flags,
        clock=state.clock,
    )
    return ui.redirect(f"/admin/orgs/{org.id}", msg="Entitlement saved.")


@router.post("/orgs/{org_id}/contracts/{contract_id}/entitlement/deactivate")
def org_entitlement_off(
    state: StateDep,
    session: SessionDep,
    ctx: Compliance,
    _csrf: ui.CsrfDep,
    org_id: uuid.UUID,
    contract_id: uuid.UUID,
) -> Response:
    org = _org(session, org_id)
    for contract, ent in contracts_mod.contracts_of(session, org.id):
        if contract.id == contract_id and ent is not None:
            contracts_mod.deactivate_entitlement(
                session, ent, principal=ctx.principal, clock=state.clock
            )
    return ui.redirect(f"/admin/orgs/{org.id}", msg="Entitlement deactivated.")


@router.post("/orgs/{org_id}/users")
def org_user(
    state: StateDep,
    session: SessionDep,
    ctx: Compliance,
    _csrf: ui.CsrfDep,
    org_id: uuid.UUID,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    role: Annotated[str, Form()] = "org_admin",
) -> Response:
    org = _org(session, org_id)
    r = Role(role)
    if r in STAFF_ROLES or r == Role.API_CLIENT:
        raise ValidationError("organisation roles only")
    users_mod.create_user(
        session,
        email=email,
        password=password,
        role=r,
        org_id=org.id,
        actor=ctx.principal.actor,
        clock=state.clock,
    )
    return ui.redirect(f"/admin/orgs/{org.id}", msg="User created.")


# --- staff users --------------------------------------------------------------------


@router.get("/users", response_class=HTMLResponse)
def staff_users(request: Request, session: SessionDep, ctx: ui.StaffDep) -> Any:
    return ui.render(
        request,
        "admin/users.html",
        ctx=ctx,
        members=users_mod.staff_users(session),
        roles=sorted(r.value for r in STAFF_ROLES),
    )


@router.post("/users")
def staff_user_create(
    state: StateDep,
    session: SessionDep,
    ctx: Admin,
    _csrf: ui.CsrfDep,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    role: Annotated[str, Form()],
) -> Response:
    r = Role(role)
    if r not in STAFF_ROLES:
        raise ValidationError("staff roles only")
    users_mod.create_user(
        session,
        email=email,
        password=password,
        role=r,
        org_id=None,
        actor=ctx.principal.actor,
        clock=state.clock,
    )
    return ui.redirect("/admin/users", msg="Staff user created.")


# --- detection rules (FR-ADM-02) ------------------------------------------------------


def _rule_form_values(request: Request) -> dict[str, Any]:
    return {
        "target_types": [t.value for t in RuleTargetType],
        "signal_types": [s.value for s in SignalType],
        "page_scopes": [p.value for p in PageScope],
        "match_kinds": sorted(MATCH_KINDS),
    }


@router.get("/rules", response_class=HTMLResponse)
def rules_list(
    request: Request,
    session: SessionDep,
    ctx: ui.StaffDep,
    q: str | None = None,
    target_type: str | None = None,
) -> Any:
    tt = RuleTargetType(target_type) if target_type else None
    rows = rule_admin.list_current(session, target_type=tt, query=q)
    return ui.render(
        request,
        "admin/rules.html",
        ctx=ctx,
        rules=rows,
        q=q or "",
        target_type=target_type or "",
        **_rule_form_values(request),
    )


@router.get("/rules/new", response_class=HTMLResponse)
def rule_new(request: Request, ctx: Analyst) -> Any:
    return ui.render(
        request,
        "admin/rule_edit.html",
        ctx=ctx,
        rule=None,
        preview=None,
        **_rule_form_values(request),
    )


@router.get("/rules/{rule_id}", response_class=HTMLResponse)
def rule_detail(request: Request, session: SessionDep, ctx: ui.StaffDep, rule_id: str) -> Any:
    versions = rule_admin.versions_of(session, rule_id)
    return ui.render(
        request,
        "admin/rule_edit.html",
        ctx=ctx,
        rule=versions[0],
        versions=versions,
        preview=None,
        **_rule_form_values(request),
    )


def _draft(
    rule_id: str,
    target_type: str,
    target_id: str,
    signal_type: str,
    pattern: str,
    weight: str,
    page_scope: str,
    match: str | None,
    enabled: str | None,
) -> rule_admin.RuleDraft:
    return rule_admin.RuleDraft(
        rule_id=rule_id.strip(),
        target_type=RuleTargetType(target_type),
        target_id=target_id.strip(),
        signal_type=SignalType(signal_type),
        pattern=pattern.strip(),
        weight=float(weight),
        page_scope=PageScope(page_scope),
        enabled=enabled is not None,
        match=(match or "").strip() or None,
    )


@router.post("/rules/preview", response_class=HTMLResponse)
def rule_preview(
    request: Request,
    state: StateDep,
    session: SessionDep,
    ctx: Analyst,
    _csrf: ui.CsrfDep,
    rule_id: Annotated[str, Form()],
    target_type: Annotated[str, Form()],
    target_id: Annotated[str, Form()],
    signal_type: Annotated[str, Form()],
    pattern: Annotated[str, Form()],
    weight: Annotated[str, Form()] = "0.5",
    page_scope: Annotated[str, Form()] = "any",
    match: Annotated[str | None, Form()] = None,
    enabled: Annotated[str | None, Form()] = None,
) -> Any:
    draft = _draft(
        rule_id, target_type, target_id, signal_type, pattern, weight, page_scope, match, enabled
    )
    result = rule_admin.preview(session, draft, ch=state.ch)
    current = rule_admin.current_version(session, draft.rule_id)
    return ui.render(
        request,
        "admin/rule_edit.html",
        ctx=ctx,
        rule=current,
        draft=draft,
        preview=result,
        versions=rule_admin.versions_of(session, draft.rule_id) if current else [],
        **_rule_form_values(request),
    )


@router.post("/rules/save")
def rule_save(
    state: StateDep,
    session: SessionDep,
    ctx: Analyst,
    _csrf: ui.CsrfDep,
    rule_id: Annotated[str, Form()],
    target_type: Annotated[str, Form()],
    target_id: Annotated[str, Form()],
    signal_type: Annotated[str, Form()],
    pattern: Annotated[str, Form()],
    weight: Annotated[str, Form()] = "0.5",
    page_scope: Annotated[str, Form()] = "any",
    match: Annotated[str | None, Form()] = None,
    enabled: Annotated[str | None, Form()] = None,
) -> Response:
    draft = _draft(
        rule_id, target_type, target_id, signal_type, pattern, weight, page_scope, match, enabled
    )
    row = rule_admin.save_version(
        session, draft, actor=ctx.principal.actor, ip=ctx.principal.ip, clock=state.clock
    )
    return ui.redirect(f"/admin/rules/{row.rule_id}", msg=f"Saved as version {row.version}.")


@router.post("/rules/{rule_id}/enabled")
def rule_enabled(
    state: StateDep,
    session: SessionDep,
    ctx: Analyst,
    _csrf: ui.CsrfDep,
    rule_id: str,
    enabled: Annotated[str, Form()],
) -> Response:
    on = enabled == "1"
    rule_admin.set_enabled(
        session, rule_id, on, actor=ctx.principal.actor, ip=ctx.principal.ip, clock=state.clock
    )
    return ui.redirect(
        f"/admin/rules/{rule_id}", msg="Rule enabled." if on else "Rule disabled (deleted)."
    )


# --- crawler monitoring (FR-ADM-03) -----------------------------------------------------


@router.get("/crawler", response_class=HTMLResponse)
def crawler(request: Request, state: StateDep, session: SessionDep, ctx: ui.StaffDep) -> Any:
    now = state.clock.now()
    day_ago = now - timedelta(hours=24)
    hour_ago = now - timedelta(hours=1)
    queue = {
        t.value: {
            "due": session.execute(
                select(func.count(ScanPlan.id)).where(
                    ScanPlan.scan_type == t,
                    ScanPlan.next_scan_at <= now,
                    (ScanPlan.locked_until.is_(None)) | (ScanPlan.locked_until < now),
                )
            ).scalar_one(),
            "locked": session.execute(
                select(func.count(ScanPlan.id)).where(
                    ScanPlan.scan_type == t, ScanPlan.locked_until >= now
                )
            ).scalar_one(),
            "total": session.execute(
                select(func.count(ScanPlan.id)).where(ScanPlan.scan_type == t)
            ).scalar_one(),
            "failing": session.execute(
                select(func.count(ScanPlan.id)).where(
                    ScanPlan.scan_type == t, ScanPlan.fail_count >= 3
                )
            ).scalar_one(),
        }
        for t in ScanType
    }
    by_status = session.execute(
        select(ScanRun.scan_type, ScanRun.status, func.count(ScanRun.id))
        .where(ScanRun.started_at >= day_ago)
        .group_by(ScanRun.scan_type, ScanRun.status)
        .order_by(ScanRun.scan_type, func.count(ScanRun.id).desc())
    ).all()
    totals = {t: 0 for t in ScanType}
    blocked = {t: 0 for t in ScanType}
    for scan_type, status, n in by_status:
        totals[scan_type] += n
        if status in (ScanStatus.BLOCKED, ScanStatus.ROBOTS_DISALLOWED):
            blocked[scan_type] += n
    throughput = {
        t.value: {
            "last_24h": totals[t],
            "per_hour": session.execute(
                select(func.count(ScanRun.id)).where(
                    ScanRun.scan_type == t, ScanRun.started_at >= hour_ago
                )
            ).scalar_one(),
            "block_share": (blocked[t] / totals[t]) if totals[t] else 0.0,
        }
        for t in ScanType
    }
    workers = session.execute(
        select(
            ScanRun.worker_id,
            func.count(ScanRun.id),
            func.max(ScanRun.started_at),
            func.avg(
                func.extract("epoch", ScanRun.finished_at)
                - func.extract("epoch", ScanRun.started_at)
            ),
        )
        .where(ScanRun.started_at >= hour_ago)
        .group_by(ScanRun.worker_id)
        .order_by(ScanRun.worker_id)
    ).all()
    return ui.render(
        request,
        "admin/crawler.html",
        ctx=ctx,
        queue=queue,
        by_status=[(t.value, s.value, n) for t, s, n in by_status],
        throughput=throughput,
        workers=workers,
    )


# --- internal domain search (FR-ADM-04) -------------------------------------------------


@router.get("/domains", response_class=HTMLResponse)
def domains(request: Request, session: SessionDep, ctx: Analyst, q: str | None = None) -> Any:
    results: list[Domain] = []
    store: StoreInternal | None = None
    if q:
        needle = q.strip().lower()
        results = list(
            session.execute(
                select(Domain)
                .where(Domain.etld1.ilike(f"%{needle}%"))
                .order_by(Domain.etld1)
                .limit(50)
            ).scalars()
        )
        exact = next((d for d in results if d.etld1 == needle), None)
        if exact is not None:
            store = _internal(session, exact)
    return ui.render(
        request, "admin/domains.html", ctx=ctx, q=q or "", results=results, store=store
    )


def _internal(session: Session, domain: Domain) -> StoreInternal | None:
    host = session.execute(
        select(Host).where(Host.domain_id == domain.id, Host.is_primary.is_(True))
    ).scalar_one_or_none()
    if host is None:
        return None
    state = read_store(session, host.id, events=30)
    sources = sorted(
        {
            s.value
            for s in session.execute(
                select(DomainSource.source).where(DomainSource.domain_id == domain.id)
            ).scalars()
        }
    )
    hosts = [
        {
            "hostname": h.third_party_etld1,
            "category": h.category,
            "provider_id": h.provider_id,
            "requests": h.request_count,
            "first_seen": h.first_seen.isoformat(),
            "last_seen": h.last_seen.isoformat(),
        }
        for h in session.execute(
            select(StoreCheckoutHost)
            .where(StoreCheckoutHost.host_id == host.id)
            .order_by(StoreCheckoutHost.category, StoreCheckoutHost.third_party_etld1)
        ).scalars()
    ]
    runs = [
        {
            "id": str(r.id),
            "type": r.scan_type.value,
            "status": r.status.value,
            "started_at": r.started_at.isoformat(),
            "stop": f"{r.stop_step or ''} {r.stop_reason or ''}".strip(),
            "worker": r.worker_id,
            "ruleset": r.ruleset_version,
            "artifacts": r.artifact_prefix,
        }
        for r in session.execute(
            select(ScanRun)
            .where(ScanRun.host_id == host.id)
            .order_by(ScanRun.started_at.desc())
            .limit(10)
        ).scalars()
    ]
    profile = state.profile if state else {}
    return StoreInternal(
        host_id=host.id,
        domain=domain.etld1,
        hostname=host.hostname,
        status=domain.status.value,
        optout=domain.optout,
        sources=sources,
        profile=profile,
        providers=state.providers if state else [],
        methods=state.methods if state else [],
        third_party_hosts=hosts,
        recent_events=state.recent_events if state else [],
        runs=runs,
        plugins=[str(h["hostname"]) for h in hosts if h["category"] == "plugin"],
        platform_version=profile.get("platform_version"),
        czds_only=sources == [DomainSourceKind.CZDS.value],
    )


# --- feature flags (FR-ADM-05) ------------------------------------------------------------


@router.get("/flags", response_class=HTMLResponse)
def flags_page(request: Request, state: StateDep, session: SessionDep, ctx: ui.StaffDep) -> Any:
    svc = FlagService(session, state.settings.flags, clock=state.clock)
    return ui.render(
        request, "admin/flags.html", ctx=ctx, flags=svc.all(), history=svc.history()[:50]
    )


@router.post("/flags/{key}")
def flag_set(
    state: StateDep,
    session: SessionDep,
    ctx: Admin,
    _csrf: ui.CsrfDep,
    key: str,
    enabled: Annotated[str, Form()],
) -> Response:
    svc = FlagService(session, state.settings.flags, clock=state.clock)
    svc.set(key, enabled == "1", actor=ctx.principal.actor, ip=ctx.principal.ip)
    return ui.redirect("/admin/flags", msg=f"{key} = {'on' if enabled == '1' else 'off'}.")


# --- manual review of findings (FR-QA-05) ---------------------------------------------------


@router.get("/review", response_class=HTMLResponse)
def review_queue(
    request: Request,
    session: SessionDep,
    ctx: Analyst,
    entity_type: str | None = None,
    entity_id: str | None = None,
    domain: str | None = None,
    max_confidence: str | None = None,
    include_reviewed: bool = False,
) -> Any:
    filters = review_mod.QueueFilters(
        entity_type=entity_type or None,
        entity_id=(entity_id or "").strip() or None,
        domain=(domain or "").strip() or None,
        max_confidence=max_confidence or None,
        include_reviewed=include_reviewed,
    )
    return ui.render(
        request,
        "admin/review.html",
        ctx=ctx,
        findings=review_mod.queue(session, filters),
        recent=review_mod.recent(session, limit=30),
        filters=filters,
        entity_types=review_mod.ENTITY_TYPES,
    )


@router.get("/review/{entity_type}/{host_id}/{entity_id}", response_class=HTMLResponse)
def review_item(
    request: Request,
    state: StateDep,
    session: SessionDep,
    ctx: Analyst,
    entity_type: str,
    host_id: int,
    entity_id: str,
) -> Any:
    finding = review_mod.get(session, entity_type, host_id, entity_id)
    rows = review_mod.evidence(state.ch, entity_type, host_id, entity_id)
    links: dict[str, str] = {}
    if state.store is not None:
        for r in rows:
            if r.evidence_key and r.evidence_key not in links:
                links[r.evidence_key] = state.store.presigned_get_url(
                    state.settings.s3.bucket_artifacts, r.evidence_key, expires_seconds=600
                )
    return ui.render(
        request,
        "admin/review_item.html",
        ctx=ctx,
        finding=finding,
        evidence=rows,
        links=links,
        ch_available=state.ch is not None,
    )


@router.post("/review/{entity_type}/{host_id}/{entity_id}")
def review_decide(
    state: StateDep,
    session: SessionDep,
    ctx: Analyst,
    _csrf: ui.CsrfDep,
    entity_type: str,
    host_id: int,
    entity_id: str,
    decision: Annotated[str, Form()],
    note: Annotated[str | None, Form()] = None,
) -> Response:
    rows = review_mod.evidence(state.ch, entity_type, host_id, entity_id)
    review_mod.decide(
        session,
        entity_type=entity_type,
        host_id=host_id,
        entity_id=entity_id,
        decision=decision,
        note=(note or "").strip() or None,
        evidence_rows=rows,
        actor=ctx.principal.actor,
        ip=ctx.principal.ip,
        clock=state.clock,
    )
    return ui.redirect("/admin/review", msg=f"Finding {decision}; gold label written.")


# --- quality (FR-QA-03, FR-QA-06) ------------------------------------------------------------


@router.get("/quality", response_class=HTMLResponse)
def quality(
    request: Request, state: StateDep, session: SessionDep, ctx: ui.StaffDep, days: int = 7
) -> Any:
    now = state.clock.now()
    dash = None
    review = None
    note = None
    if state.ch is not None:
        try:
            dash = dash_mod.dashboard(
                session,
                state.ch,
                now=now,
                days=days,
                light_cycle_days=state.settings.scan.light_interval_days_ecommerce,
                checkout_cycle_days=state.settings.scan.checkout_interval_days,
            )
            review = stops_mod.review(state.ch, now=now, days=days)
        except Exception as exc:  # ClickHouse down: the page still renders alerts
            note = f"ClickHouse unavailable: {exc}"[:200]
    else:
        note = "ClickHouse is not configured for this process."
    alerts = list(
        session.execute(
            select(QualityAlert).order_by(QualityAlert.detected_at.desc()).limit(50)
        ).scalars()
    )
    held = {
        a.id: anomalies_mod.held_count(session, a.id)
        for a in alerts
        if a.kind == anomalies_mod.KIND_REMOVED_SPIKE
    }
    return ui.render(
        request,
        "admin/quality.html",
        ctx=ctx,
        dash=dash,
        review=review,
        alerts=alerts,
        held=held,
        note=note,
        days=days,
        gold=gold_size(session),
        gold_target=state.settings.quality.gold_set_target_size,
    )


@router.post("/quality/alerts/{alert_id}/ack")
def quality_ack(
    state: StateDep, session: SessionDep, ctx: ui.StaffDep, _csrf: ui.CsrfDep, alert_id: int
) -> Response:
    """Acknowledge; for FR-QA-04 spikes this confirms the removals and releases held alerts."""
    released = anomalies_mod.confirm(
        session, alert_id, actor=ctx.principal.actor, ip=ctx.principal.ip, clock=state.clock
    )
    msg = "Alert acknowledged."
    if released:
        msg = f"Alert confirmed; {released} held client alert(s) released."
    return ui.redirect("/admin/quality", msg=msg)


@router.post("/quality/alerts/{alert_id}/discard")
def quality_discard(
    state: StateDep, session: SessionDep, ctx: ui.StaffDep, _csrf: ui.CsrfDep, alert_id: int
) -> Response:
    """FR-QA-04: the spike was a detection artefact — held client alerts are not sent."""
    dropped = anomalies_mod.discard(
        session, alert_id, actor=ctx.principal.actor, ip=ctx.principal.ip, clock=state.clock
    )
    return ui.redirect(
        "/admin/quality", msg=f"Alert discarded; {dropped} held client alert(s) dropped."
    )


# --- opt-out and DSAR (FR-OO-02/03) ---------------------------------------------------------


@router.get("/optout", response_class=HTMLResponse)
def optout_page(request: Request, session: SessionDep, ctx: ui.StaffDep) -> Any:
    from payintel.core.models.audit import OptoutRequest

    recent = list(
        session.execute(
            select(OptoutRequest)
            .where(OptoutRequest.verified_at.is_not(None))
            .order_by(OptoutRequest.verified_at.desc())
            .limit(50)
        ).scalars()
    )
    return ui.render(
        request,
        "admin/optout.html",
        ctx=ctx,
        pending=optout_mod.pending_requests(session),
        recent=recent,
    )


@router.post("/optout/{request_id}/verify")
def optout_verify(
    state: StateDep, session: SessionDep, ctx: Compliance, _csrf: ui.CsrfDep, request_id: int
) -> Response:
    from payintel.core.models.audit import OptoutRequest

    req = session.get(OptoutRequest, request_id)
    if req is None:
        raise NotFoundError("request not found")
    if state.dns_txt is None or state.http_get is None:
        raise ConfigurationError("opt-out verification resolvers are not configured")
    ok = optout_mod.verify(
        session, req, dns_txt=state.dns_txt, http_get=state.http_get, clock=state.clock
    )
    applied = optout_mod.apply_verified(session, clock=state.clock) if ok else 0
    msg = f"Verified and applied ({applied})." if ok else "Proof not found."
    return ui.redirect("/admin/optout", msg=msg)


@router.post("/optout/{request_id}/manual")
def optout_manual(
    state: StateDep, session: SessionDep, ctx: Compliance, _csrf: ui.CsrfDep, request_id: int
) -> Response:
    from payintel.core.models.audit import OptoutRequest

    req = session.get(OptoutRequest, request_id)
    if req is None:
        raise NotFoundError("request not found")
    optout_mod.manual_verify(session, req, actor=ctx.principal.actor, clock=state.clock)
    optout_mod.apply_verified(session, clock=state.clock)
    return ui.redirect("/admin/optout", msg="Marked verified manually and applied.")


@router.get("/dsar", response_class=HTMLResponse)
def dsar_page(request: Request, state: StateDep, session: SessionDep, ctx: ui.StaffDep) -> Any:
    from payintel.core.models.portal import DsarRequest

    closed = list(
        session.execute(
            select(DsarRequest)
            .where(DsarRequest.status == DsarStatus.CLOSED)
            .order_by(DsarRequest.closed_at.desc())
            .limit(30)
        ).scalars()
    )
    overdue_ids = {r.id for r in dsar_mod.overdue(session, clock=state.clock)}
    return ui.render(
        request,
        "admin/dsar.html",
        ctx=ctx,
        open_rows=dsar_mod.open_requests(session),
        closed=closed,
        overdue_ids=overdue_ids,
    )


@router.post("/dsar/{request_id}/take")
def dsar_take(
    state: StateDep, session: SessionDep, ctx: Compliance, _csrf: ui.CsrfDep, request_id: int
) -> Response:
    from payintel.core.models.portal import DsarRequest

    req = session.get(DsarRequest, request_id)
    if req is None:
        raise NotFoundError("request not found")
    dsar_mod.take(session, req, principal=ctx.principal, clock=state.clock)
    return ui.redirect("/admin/dsar", msg="Request taken.")


@router.post("/dsar/{request_id}/close")
def dsar_close(
    state: StateDep,
    session: SessionDep,
    ctx: Compliance,
    _csrf: ui.CsrfDep,
    request_id: int,
    resolution: Annotated[str, Form()],
) -> Response:
    from payintel.core.models.portal import DsarRequest

    req = session.get(DsarRequest, request_id)
    if req is None:
        raise NotFoundError("request not found")
    dsar_mod.close(session, req, principal=ctx.principal, resolution=resolution, clock=state.clock)
    return ui.redirect("/admin/dsar", msg="Request closed.")


# --- anti-abuse (FR-AB-02…05) ------------------------------------------------------------------


@router.get("/abuse", response_class=HTMLResponse)
def abuse_page(request: Request, session: SessionDep, ctx: Compliance) -> Any:
    orgs = {o.id: o for o in session.execute(select(Organization)).scalars()}
    closed = list(
        session.execute(
            select(AbuseIncident)
            .where(AbuseIncident.status != "open")
            .order_by(AbuseIncident.resolved_at.desc())
            .limit(30)
        ).scalars()
    )
    return ui.render(
        request,
        "admin/abuse.html",
        ctx=ctx,
        open_incidents=incidents_mod.open_incidents(session),
        closed=closed,
        orgs=orgs,
        restricted=[o for o in orgs.values() if o.restricted_at is not None],
        hits=canary_mod.recent_hits(session, limit=50),
    )


@router.post("/abuse/incidents/{incident_id}/resolve")
def abuse_resolve(
    state: StateDep,
    session: SessionDep,
    ctx: Compliance,
    _csrf: ui.CsrfDep,
    incident_id: int,
    status: Annotated[str, Form()] = "resolved",
    resolution: Annotated[str | None, Form()] = None,
    lift_restriction: Annotated[str | None, Form()] = None,
) -> Response:
    incidents_mod.resolve(
        session,
        incident_id,
        status=status,
        resolution=(resolution or "").strip() or None,
        lift_restriction=bool(lift_restriction),
        actor=ctx.principal.actor,
        ip=ctx.principal.ip,
        clock=state.clock,
    )
    return ui.redirect("/admin/abuse", msg=f"Incident {status}.")


@router.post("/orgs/{org_id}/restrict")
def org_restrict(
    state: StateDep,
    session: SessionDep,
    ctx: Compliance,
    _csrf: ui.CsrfDep,
    org_id: uuid.UUID,
    reason: Annotated[str, Form()],
) -> Response:
    org = _org(session, org_id)
    if not reason.strip():
        raise ValidationError("a reason is required")
    incidents_mod.restrict(
        session, org, reason=reason.strip(), actor=ctx.principal.actor, clock=state.clock
    )
    return ui.redirect(f"/admin/orgs/{org.id}", msg="API access restricted.")


@router.post("/orgs/{org_id}/unrestrict")
def org_unrestrict(
    state: StateDep, session: SessionDep, ctx: Compliance, _csrf: ui.CsrfDep, org_id: uuid.UUID
) -> Response:
    org = _org(session, org_id)
    if org.restricted_at is not None:
        incidents_mod.unrestrict(session, org, actor=ctx.principal.actor, clock=state.clock)
    return ui.redirect(f"/admin/orgs/{org.id}", msg="API access restored.")


@router.post("/orgs/{org_id}/usage-report")
def org_usage_report(
    state: StateDep,
    session: SessionDep,
    ctx: ui.StaffDep,
    _csrf: ui.CsrfDep,
    org_id: uuid.UUID,
    period: Annotated[str | None, Form()] = None,
) -> Response:
    org = _org(session, org_id)
    now = state.clock.now()
    quarter = usage_mod.Quarter.of(now.date()).previous()
    if period:
        year, _, q = period.upper().partition("-Q")
        quarter = usage_mod.Quarter(int(year), int(q))
    usage_mod.build(
        session,
        org,
        quarter,
        store=state.store,
        bucket=state.settings.s3.bucket_exports,
        now=now,
    )
    return ui.redirect(f"/admin/orgs/{org.id}", msg=f"Usage report {quarter.period} built.")


# --- export approvals (FR-EX-06) ------------------------------------------------------------


@router.get("/exports", response_class=HTMLResponse)
def exports_page(request: Request, session: SessionDep, ctx: ui.StaffDep) -> Any:
    awaiting = list(
        session.execute(
            select(ExportJob, Organization)
            .join(Organization, Organization.id == ExportJob.org_id)
            .where(ExportJob.status == ExportStatus.AWAITING_APPROVAL)
            .order_by(ExportJob.created_at)
        ).all()
    )
    recent = list(
        session.execute(
            select(ExportJob, Organization)
            .join(Organization, Organization.id == ExportJob.org_id)
            .where(ExportJob.status != ExportStatus.AWAITING_APPROVAL)
            .order_by(ExportJob.created_at.desc())
            .limit(50)
        ).all()
    )
    return ui.render(request, "admin/exports.html", ctx=ctx, awaiting=awaiting, recent=recent)


@router.post("/exports/{export_id}/approve")
def export_approve(
    state: StateDep, session: SessionDep, ctx: Compliance, _csrf: ui.CsrfDep, export_id: uuid.UUID
) -> Response:
    job = session.get(ExportJob, export_id)
    if job is None:
        raise NotFoundError("export not found")
    exports.approve(session, job, principal=ctx.principal, clock=state.clock)
    return ui.redirect("/admin/exports", msg="Export approved; the exporter will build it.")


# --- report constructor (FR-RP-01) ------------------------------------------------------------


@router.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request, state: StateDep, session: SessionDep, ctx: ui.StaffDep) -> Any:
    orgs = list(
        session.execute(
            select(Organization)
            .where(Organization.status == OrgStatus.ACTIVE)
            .order_by(Organization.legal_name)
        ).scalars()
    )
    return ui.render(
        request,
        "admin/reports.html",
        ctx=ctx,
        jobs=reports.recent(session, limit=30),
        metrics=aggregates.METRICS,
        min_cell=state.settings.quality.report_min_cell_size,
        orgs=orgs,
    )


@router.post("/reports")
def report_build(
    state: StateDep,
    session: SessionDep,
    ctx: Analyst,
    _csrf: ui.CsrfDep,
    countries: Annotated[str, Form()],
    platforms: Annotated[str | None, Form()] = None,
    verticals: Annotated[str | None, Form()] = None,
    period_start: Annotated[str | None, Form()] = None,
    period_end: Annotated[str | None, Form()] = None,
    metrics: Annotated[list[str], Form()] = [],  # noqa: B006 - form default
    org_id: Annotated[str | None, Form()] = None,
) -> Response:
    chosen = tuple(m for m in aggregates.METRICS if m in metrics) or aggregates.METRICS
    spec = aggregates.ReportSpec(
        countries=tuple(c.upper() for c in ui.parse_list(countries)),
        platforms=tuple(ui.parse_list(platforms)),
        verticals=tuple(ui.parse_list(verticals)),
        period_start=date.fromisoformat(period_start) if period_start else None,
        period_end=date.fromisoformat(period_end) if period_end else None,
        metrics=chosen,
        min_cell=state.settings.quality.report_min_cell_size,
    )
    if not spec.countries:
        raise ValidationError("at least one country is required")
    job, _xlsx, _csv = reports.build_report(
        session,
        spec,
        principal=ctx.principal,
        store=state.store,
        settings=state.settings,
        clock=state.clock,
        ch=state.ch,
        org_id=uuid.UUID(org_id) if org_id else None,
    )
    return ui.redirect("/admin/reports", msg=f"Report {job.id} built: {job.status.value}.")


@router.get("/reports/{job_id}/{kind}")
def report_download(
    state: StateDep, session: SessionDep, ctx: ui.StaffDep, job_id: uuid.UUID, kind: str
) -> Response:
    job = reports.get_job(session, job_id)
    if state.store is None:
        raise ConfigurationError("object store is not configured")
    key = job.xlsx_key if kind == "xlsx" else job.csv_key if kind == "csv" else None
    if not key:
        raise NotFoundError("file not available")
    data = state.store.get_bytes(state.settings.s3.bucket_exports, key)
    media = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if kind == "xlsx"
        else "application/zip"
    )
    filename = f"payintel-report-{job.id}.{'xlsx' if kind == 'xlsx' else 'csv.zip'}"
    return Response(
        data,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --- audit log (FR-AB-01) -------------------------------------------------------------------------


@router.get("/audit", response_class=HTMLResponse)
def audit_page(
    request: Request,
    session: SessionDep,
    ctx: ui.StaffDep,
    actor: str | None = None,
    action: str | None = None,
    verify: str | None = None,
) -> Any:
    stmt = select(AuditLog).order_by(AuditLog.id.desc()).limit(200)
    if actor:
        stmt = stmt.where(AuditLog.actor.ilike(f"%{actor.strip()}%"))
    if action:
        stmt = stmt.where(AuditLog.action.ilike(f"%{action.strip()}%"))
    rows = list(session.execute(stmt).scalars())
    chain = audit.verify_chain(session) if verify else None
    return ui.render(
        request,
        "admin/audit.html",
        ctx=ctx,
        rows=rows,
        actor=actor or "",
        action=action or "",
        chain=chain,
    )


_ = require_staff  # role checks are expressed through the `staff_roles` dependencies above
