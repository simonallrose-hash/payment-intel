"""Client portal (FR-UI-01…05): login + TOTP, search, store card, watchlists,
alerts, exports, reports, API keys, users, usage, API docs.

Every page uses the same read layer and entitlements as `/v1` (`read.*`,
`Grant`), so a portal user never sees more than the organisation's API key
would return (FR-API-06/08). State-changing forms carry the session CSRF token.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlalchemy import func, select
from starlette.responses import HTMLResponse, RedirectResponse, Response

from payintel.alerts import rules as rules_mod
from payintel.alerts import watchlists as wl_mod
from payintel.alerts import webhook as webhook_mod
from payintel.alerts.delivery import deliveries_of_org
from payintel.api import read, ui
from payintel.api.auth import keys as keys_mod
from payintel.api.auth import passwords, sessions, totp
from payintel.api.deps import SessionDep, StateDep, client_ip
from payintel.compliance import users as users_mod
from payintel.core import audit
from payintel.core.errors import ConfigurationError, NotFoundError, ValidationError
from payintel.core.models.access import ApiKey, User
from payintel.core.models.audit import UsageLog
from payintel.core.models.base import (
    ChangeEventType,
    ExportFormat,
    ExportStatus,
    ExportType,
    Role,
)
from payintel.core.models.orgs import Organization
from payintel.core.models.scans import ScanRun
from payintel.entitlements.check import require_role
from payintel.entitlements.model import ORG_ROLES, SCOPES
from payintel.entitlements.quotas import _day_start, _month_start, records_used
from payintel.exports import service as exports
from payintel.reports import service as reports

router = APIRouter(prefix="/portal", tags=["portal"], include_in_schema=False)

PAGE = 50


def _safe_next(raw: str | None) -> str:
    if raw and raw.startswith("/") and not raw.startswith("//"):
        return raw
    return "/portal"


# --- authentication ---------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str | None = None) -> HTMLResponse:
    return ui.render(request, "portal/login.html", next=_safe_next(next))


@router.post("/login", dependencies=[Depends(ui.same_origin_guard)])
def login(
    request: Request,
    state: StateDep,
    session: SessionDep,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: Annotated[str | None, Form()] = None,
) -> Response:
    now = state.clock.now()
    ip = client_ip(request)
    user = session.execute(
        select(User).where(User.email == email.strip().lower())
    ).scalar_one_or_none()
    generic = "Invalid e-mail or password."
    if user is None:
        audit.record(
            session,
            actor=f"anonymous:{email.strip().lower()[:100]}",
            action="auth.login_failed",
            object_type="user",
            object_id="unknown",
            ip=ip,
            clock=state.clock,
        )
        return ui.render(request, "portal/login.html", status=401, error=generic, next=next)
    if passwords.is_locked(user, now):
        return ui.render(
            request,
            "portal/login.html",
            status=423,
            error="Too many failed attempts. Try again later.",
            next=next,
        )
    if not passwords.verify_password(user.password_hash, password):
        passwords.register_failure(
            user,
            now=now,
            max_attempts=state.settings.api.login_max_attempts,
            window_minutes=state.settings.api.login_attempt_window_minutes,
        )
        audit.record(
            session,
            actor=f"anonymous:{user.email}",
            action="auth.login_failed",
            object_type="user",
            object_id=str(user.id),
            after={"failed_logins": user.failed_logins},
            ip=ip,
            clock=state.clock,
        )
        return ui.render(request, "portal/login.html", status=401, error=generic, next=next)
    passwords.register_success(user, now=now)
    token, _row = sessions.create_session(session, user_id=user.id, ip=ip, now=now)
    audit.record(
        session,
        actor=f"user:{user.email}",
        action="auth.password_ok",
        object_type="user",
        object_id=str(user.id),
        ip=ip,
        clock=state.clock,
    )
    target = "/portal/login/totp" if totp.enrolled(user) else "/portal/login/enrol"
    response = ui.redirect(f"{target}?next={_safe_next(next)}")
    ui.set_session_cookie(response, token, state)
    return response


@router.get("/login/enrol", response_class=HTMLResponse)
def enrol_form(request: Request, state: StateDep, ctx: ui.PendingDep) -> Response:
    if totp.enrolled(ctx.user):
        return ui.redirect("/portal/login/totp")
    secret = request.query_params.get("s")
    if not secret:
        secret = totp.new_secret()
        return ui.redirect(f"/portal/login/enrol?s={secret}")
    uri = totp.provisioning_uri(secret, email=ctx.user.email, issuer=state.settings.api.totp_issuer)
    return ui.render(request, "portal/enrol.html", ctx=ctx, secret=secret, uri=uri)


@router.post("/login/enrol")
def enrol(
    request: Request,
    state: StateDep,
    session: SessionDep,
    ctx: ui.PendingDep,
    _csrf: ui.CsrfDep,
    secret: Annotated[str, Form()],
    code: Annotated[str, Form()],
) -> Response:
    if totp.enrolled(ctx.user):
        return ui.redirect("/portal/login/totp")
    import pyotp

    if not pyotp.TOTP(secret).verify(code.strip(), for_time=state.clock.now(), valid_window=1):
        uri = totp.provisioning_uri(
            secret, email=ctx.user.email, issuer=state.settings.api.totp_issuer
        )
        return ui.render(
            request,
            "portal/enrol.html",
            status=400,
            ctx=ctx,
            secret=secret,
            uri=uri,
            error="Code did not match. Scan the secret again and retry.",
        )
    totp.store_secret(ctx.user, secret, state.secret_box)
    ctx.user.totp_confirmed_at = state.clock.now()
    ctx.session_row.totp_verified = True
    session.flush()
    audit.record(
        session,
        actor=ctx.principal.actor,
        action="auth.totp_enrolled",
        object_type="user",
        object_id=str(ctx.user.id),
        ip=ctx.principal.ip,
        clock=state.clock,
    )
    return ui.redirect("/portal", msg="Two-factor authentication enabled.")


@router.get("/login/totp", response_class=HTMLResponse)
def totp_form(request: Request, ctx: ui.PendingDep, next: str | None = None) -> Response:
    if ctx.session_row.totp_verified:
        return ui.redirect(_safe_next(next))
    if not totp.enrolled(ctx.user):
        return ui.redirect("/portal/login/enrol")
    return ui.render(request, "portal/totp.html", ctx=ctx, next=_safe_next(next))


@router.post("/login/totp")
def totp_verify(
    request: Request,
    state: StateDep,
    session: SessionDep,
    ctx: ui.PendingDep,
    _csrf: ui.CsrfDep,
    code: Annotated[str, Form()],
    next: Annotated[str | None, Form()] = None,
) -> Response:
    now = state.clock.now()
    if passwords.is_locked(ctx.user, now):
        return ui.render(
            request, "portal/totp.html", status=423, ctx=ctx, error="Locked. Try again later."
        )
    if not totp.verify_code(ctx.user, code.strip(), state.secret_box, at=now):
        passwords.register_failure(
            ctx.user,
            now=now,
            max_attempts=state.settings.api.login_max_attempts,
            window_minutes=state.settings.api.login_attempt_window_minutes,
        )
        audit.record(
            session,
            actor=ctx.principal.actor,
            action="auth.totp_failed",
            object_type="user",
            object_id=str(ctx.user.id),
            ip=ctx.principal.ip,
            clock=state.clock,
        )
        return ui.render(
            request, "portal/totp.html", status=401, ctx=ctx, error="Wrong code.", next=next
        )
    passwords.register_success(ctx.user, now=now)
    ctx.session_row.totp_verified = True
    session.flush()
    audit.record(
        session,
        actor=ctx.principal.actor,
        action="auth.login",
        object_type="user",
        object_id=str(ctx.user.id),
        ip=ctx.principal.ip,
        clock=state.clock,
    )
    return ui.redirect(_safe_next(next))


@router.post("/logout")
def logout(state: StateDep, session: SessionDep, ctx: ui.PendingDep, _csrf: ui.CsrfDep) -> Response:
    sessions.revoke(session, ctx.session_row, now=state.clock.now())
    audit.record(
        session,
        actor=ctx.principal.actor,
        action="auth.logout",
        object_type="user",
        object_id=str(ctx.user.id),
        ip=ctx.principal.ip,
        clock=state.clock,
    )
    response = ui.redirect("/portal/login", msg="Signed out.")
    ui.clear_session_cookie(response)
    return response


# --- home -------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
def home(request: Request, state: StateDep, session: SessionDep, ctx: ui.UserDep) -> Response:
    if ctx.is_staff:
        return ui.redirect("/admin")
    org = session.get(Organization, ctx.org_id) if ctx.org_id else None
    now = state.clock.now()
    usage = None
    if ctx.grant is not None and ctx.org_id is not None:
        usage = {
            "day": records_used(session, ctx.org_id, since=_day_start(now)),
            "month": records_used(session, ctx.org_id, since=_month_start(now)),
        }
    return ui.render(request, "portal/home.html", ctx=ctx, org=org, usage=usage)


# --- search and store card --------------------------------------------------


def _filters(q: dict[str, str]) -> read.StoreFilters:
    def d(key: str) -> date | None:
        v = q.get(key)
        return date.fromisoformat(v) if v else None

    def dt(key: str) -> datetime | None:
        v = q.get(key)
        return datetime.fromisoformat(v) if v else None

    conf = q.get("min_confidence") or None
    if conf is not None and conf not in ("high", "medium", "low"):
        raise ValidationError("min_confidence must be high|medium|low")
    return read.StoreFilters(
        countries=ui.parse_list(q.get("country")),
        platforms=ui.parse_list(q.get("platform")),
        providers=ui.parse_list(q.get("provider")),
        without_providers=ui.parse_list(q.get("without_provider")),
        provider_roles=ui.parse_list(q.get("provider_role")),
        methods=ui.parse_list(q.get("payment_method")),
        providers_min=ui.parse_int(q.get("providers_min")),
        providers_max=ui.parse_int(q.get("providers_max")),
        min_confidence=conf,
        first_seen_from=d("first_seen_from"),
        first_seen_to=d("first_seen_to"),
        last_seen_from=d("last_seen_from"),
        last_seen_to=d("last_seen_to"),
        changed_from=dt("changed_from"),
        changed_to=dt("changed_to"),
        domain_prefix=q.get("domain_prefix") or None,
    )


@router.get("/stores", response_class=HTMLResponse)
def stores(request: Request, state: StateDep, session: SessionDep, ctx: ui.OrgDep) -> HTMLResponse:
    grant = ui.require_grant(ctx)
    q = dict(request.query_params)
    items: list[Any] = []
    nxt: str | None = None
    searched = bool(q)
    if searched:
        rows, nxt = read.search(
            session,
            grant,
            _filters(q),
            sort=q.get("sort") or "domain",
            cursor=q.get("cursor") or None,
            limit=PAGE,
        )
        items = [
            read.build_store(
                session,
                r,
                profile=grant.profile,
                methodology_url=state.settings.api.methodology_url,
                evidence=state.evidence,
                min_confidence=q.get("min_confidence") or None,
            ).model_dump()
            for r in rows
        ]
    next_query = ""
    if nxt:
        params = {k: v for k, v in q.items() if k != "cursor"}
        params["cursor"] = nxt
        from urllib.parse import urlencode

        next_query = urlencode(params)
    return ui.render(
        request,
        "portal/stores.html",
        ctx=ctx,
        q=q,
        items=items,
        next_query=next_query,
        searched=searched,
        grant=grant,
    )


@router.get("/stores/{domain}", response_class=HTMLResponse)
def store_card(
    request: Request, state: StateDep, session: SessionDep, ctx: ui.OrgDep, domain: str
) -> HTMLResponse:
    grant = ui.require_grant(ctx)
    row = read.lookup(session, grant, domain)
    store = read.build_store(
        session,
        row,
        profile=grant.profile,
        methodology_url=state.settings.api.methodology_url,
        evidence=state.evidence,
        evidence_limit=5,
    ).model_dump()
    history, _ = read.history(session, row, profile=grant.profile, limit=100, cursor=None)
    run = session.execute(
        select(ScanRun)
        .where(ScanRun.host_id == row.host_id, ScanRun.artifact_prefix.is_not(None))
        .order_by(ScanRun.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    has_screenshot = run is not None and state.store is not None
    return ui.render(
        request,
        "portal/store.html",
        ctx=ctx,
        store=store,
        history=[h.model_dump() for h in history],
        profile=grant.profile.value,
        has_screenshot=has_screenshot,
    )


@router.get("/stores/{domain}/screenshot")
def store_screenshot(state: StateDep, session: SessionDep, ctx: ui.OrgDep, domain: str) -> Response:
    """Checkout screenshot of the latest scan, proxied from S3 (same origin → CSP img-src)."""
    grant = ui.require_grant(ctx)
    row = read.lookup(session, grant, domain)
    if state.store is None:
        raise NotFoundError("no artefact store")
    runs = session.execute(
        select(ScanRun)
        .where(ScanRun.host_id == row.host_id, ScanRun.artifact_prefix.is_not(None))
        .order_by(ScanRun.started_at.desc())
        .limit(5)
    ).scalars()
    bucket = state.settings.s3.bucket_artifacts
    for run in runs:
        for name in ("screenshot.jpg", "stop_screenshot.jpg"):
            data = _get_or_none(state.store, bucket, f"{run.artifact_prefix}{name}")
            if data is not None:
                return Response(data, media_type="image/jpeg")
    raise NotFoundError("no screenshot stored for this store")


def _get_or_none(store: Any, bucket: str, key: str) -> bytes | None:
    try:
        data: bytes = store.get_bytes(bucket, key)
    except Exception:  # missing key or storage error: the card simply has no image
        return None
    return data


# --- watchlists ---------------------------------------------------------------


@router.get("/watchlists", response_class=HTMLResponse)
def watchlists(request: Request, session: SessionDep, ctx: ui.OrgDep) -> HTMLResponse:
    grant = ui.require_grant(ctx)
    from payintel.core.models.alerts import Watchlist

    rows = session.execute(
        select(Watchlist).where(Watchlist.org_id == grant.org_id).order_by(Watchlist.name)
    ).scalars()
    lists = [(w, wl_mod.item_count(session, w.id)) for w in rows]
    return ui.render(
        request,
        "portal/watchlists.html",
        ctx=ctx,
        lists=lists,
        total=wl_mod.org_total(session, grant.org_id),
        limit=grant.watchlist_limit,
    )


@router.post("/watchlists")
def watchlist_create(
    state: StateDep,
    session: SessionDep,
    ctx: ui.OrgDep,
    _csrf: ui.CsrfDep,
    name: Annotated[str, Form()],
) -> Response:
    grant = ui.require_grant(ctx)
    require_role(ctx.principal, Role.ORG_ANALYST)
    wl = wl_mod.create_watchlist(
        session,
        org_id=grant.org_id,
        name=name.strip(),
        actor=ctx.principal.actor,
        clock=state.clock,
    )
    return ui.redirect(f"/portal/watchlists/{wl.id}", msg="Watchlist created.")


@router.get("/watchlists/{watchlist_id}", response_class=HTMLResponse)
def watchlist_detail(
    request: Request, session: SessionDep, ctx: ui.OrgDep, watchlist_id: int
) -> HTMLResponse:
    grant = ui.require_grant(ctx)
    wl = wl_mod.get_watchlist(session, grant.org_id, watchlist_id)
    from payintel.core.models.alerts import WatchlistItem

    items = list(
        session.execute(
            select(WatchlistItem)
            .where(WatchlistItem.watchlist_id == wl.id)
            .order_by(WatchlistItem.domain)
            .limit(5000)
        ).scalars()
    )
    return ui.render(request, "portal/watchlist.html", ctx=ctx, wl=wl, items=items)


@router.post("/watchlists/{watchlist_id}/domains")
async def watchlist_add(
    state: StateDep,
    session: SessionDep,
    ctx: ui.OrgDep,
    _csrf: ui.CsrfDep,
    watchlist_id: int,
    domains: Annotated[str | None, Form()] = None,
    csv_file: Annotated[UploadFile | None, File()] = None,
) -> Response:
    grant = ui.require_grant(ctx)
    require_role(ctx.principal, Role.ORG_ANALYST)
    wl = wl_mod.get_watchlist(session, grant.org_id, watchlist_id)
    raw = ui.parse_list(domains)
    if csv_file is not None and csv_file.filename:
        text = (await csv_file.read()).decode("utf-8", errors="replace")
        raw.extend(wl_mod.parse_csv(text))
    result = wl_mod.add_domains(
        session, wl, raw, limit=grant.watchlist_limit, actor=ctx.principal.actor, clock=state.clock
    )
    return ui.redirect(
        f"/portal/watchlists/{wl.id}",
        msg=f"Added {result.added}, skipped {result.skipped} (invalid or duplicate).",
    )


@router.post("/watchlists/{watchlist_id}/remove")
def watchlist_remove(
    session: SessionDep,
    ctx: ui.OrgDep,
    _csrf: ui.CsrfDep,
    watchlist_id: int,
    domain: Annotated[str, Form()],
) -> Response:
    grant = ui.require_grant(ctx)
    require_role(ctx.principal, Role.ORG_ANALYST)
    wl = wl_mod.get_watchlist(session, grant.org_id, watchlist_id)
    wl_mod.remove_domain(session, wl, domain)
    return ui.redirect(f"/portal/watchlists/{wl.id}", msg="Domain removed.")


@router.post("/watchlists/{watchlist_id}/delete")
def watchlist_delete(
    state: StateDep, session: SessionDep, ctx: ui.OrgDep, _csrf: ui.CsrfDep, watchlist_id: int
) -> Response:
    grant = ui.require_grant(ctx)
    require_role(ctx.principal, Role.ORG_ANALYST)
    wl = wl_mod.get_watchlist(session, grant.org_id, watchlist_id)
    wl_mod.delete_watchlist(session, wl, actor=ctx.principal.actor, clock=state.clock)
    return ui.redirect("/portal/watchlists", msg="Watchlist deleted.")


# --- alerts -------------------------------------------------------------------


@router.get("/alerts", response_class=HTMLResponse)
def alerts(request: Request, session: SessionDep, ctx: ui.OrgDep) -> HTMLResponse:
    grant = ui.require_grant(ctx)
    from payintel.core.models.alerts import Watchlist

    lists = list(
        session.execute(
            select(Watchlist).where(Watchlist.org_id == grant.org_id).order_by(Watchlist.name)
        ).scalars()
    )
    return ui.render(
        request,
        "portal/alerts.html",
        ctx=ctx,
        rules=rules_mod.rules_of(session, grant.org_id),
        hooks=webhook_mod.list_webhooks(session, grant.org_id),
        lists=lists,
        deliveries=deliveries_of_org(session, grant.org_id, limit=50),
        event_types=[e.value for e in ChangeEventType],
        channels=sorted(rules_mod.CHANNELS),
        digests=sorted(rules_mod.DIGESTS),
        segment_countries=sorted(grant.countries) or ["all"],
        segment_platforms=sorted(grant.platforms) or ["all"],
    )


@router.post("/alerts/rules")
def rule_create(
    request: Request,
    state: StateDep,
    session: SessionDep,
    ctx: ui.OrgDep,
    _csrf: ui.CsrfDep,
    channel: Annotated[str, Form()],
    digest: Annotated[str, Form()] = "immediate",
    min_confidence: Annotated[str, Form()] = "medium",
    watchlist_id: Annotated[str | None, Form()] = None,
    provider_id: Annotated[str | None, Form()] = None,
    method_id: Annotated[str | None, Form()] = None,
    webhook_id: Annotated[str | None, Form()] = None,
    telegram_chat_id: Annotated[str | None, Form()] = None,
    event_types: Annotated[str | None, Form()] = None,
    countries: Annotated[str | None, Form()] = None,
    platforms: Annotated[str | None, Form()] = None,
) -> Response:
    grant = ui.require_grant(ctx)
    require_role(ctx.principal, Role.ORG_ANALYST)
    rules_mod.create_rule(
        session,
        org_id=grant.org_id,
        watchlist_id=ui.parse_int(watchlist_id),
        event_types=ui.parse_list(event_types),
        provider_id=(provider_id or "").strip() or None,
        method_id=(method_id or "").strip() or None,
        min_confidence=min_confidence,
        channel=channel,
        webhook_id=ui.parse_int(webhook_id),
        telegram_chat_id=(telegram_chat_id or "").strip() or None,
        digest=digest,
        actor=ctx.principal.actor,
        clock=state.clock,
        countries=ui.parse_list(countries),
        platforms=ui.parse_list(platforms),
        grant=grant,
    )
    return ui.redirect("/portal/alerts", msg="Alert rule created.")


@router.post("/alerts/rules/{rule_id}/toggle")
def rule_toggle(
    state: StateDep, session: SessionDep, ctx: ui.OrgDep, _csrf: ui.CsrfDep, rule_id: int
) -> Response:
    grant = ui.require_grant(ctx)
    require_role(ctx.principal, Role.ORG_ANALYST)
    rule = rules_mod.get_rule(session, grant.org_id, rule_id)
    rules_mod.set_enabled(
        session, rule, not rule.enabled, actor=ctx.principal.actor, clock=state.clock
    )
    return ui.redirect("/portal/alerts", msg="Rule updated.")


@router.post("/alerts/rules/{rule_id}/delete")
def rule_delete(
    state: StateDep, session: SessionDep, ctx: ui.OrgDep, _csrf: ui.CsrfDep, rule_id: int
) -> Response:
    grant = ui.require_grant(ctx)
    require_role(ctx.principal, Role.ORG_ANALYST)
    rule = rules_mod.get_rule(session, grant.org_id, rule_id)
    rules_mod.delete_rule(session, rule, actor=ctx.principal.actor, clock=state.clock)
    return ui.redirect("/portal/alerts", msg="Rule deleted.")


@router.post("/alerts/webhooks")
def webhook_create(
    request: Request,
    state: StateDep,
    session: SessionDep,
    ctx: ui.OrgDep,
    _csrf: ui.CsrfDep,
    url: Annotated[str, Form()],
) -> Response:
    grant = ui.require_grant(ctx)
    require_role(ctx.principal, Role.ORG_ADMIN)
    hook, secret = webhook_mod.create_webhook(
        session,
        org_id=grant.org_id,
        url=url.strip(),
        enabled=True,
        box=state.secret_box,
        actor=ctx.principal.actor,
        clock=state.clock,
    )
    return ui.render(
        request,
        "portal/secret_once.html",
        ctx=ctx,
        title="Webhook created",
        label="Signing secret (shown once)",
        secret=secret,
        back="/portal/alerts",
        note=f"Webhook #{hook.id} → {hook.url}. Verify `X-PayIntel-Signature` with this secret.",
    )


@router.post("/alerts/webhooks/{webhook_id}/delete")
def webhook_delete(
    state: StateDep, session: SessionDep, ctx: ui.OrgDep, _csrf: ui.CsrfDep, webhook_id: int
) -> Response:
    grant = ui.require_grant(ctx)
    require_role(ctx.principal, Role.ORG_ADMIN)
    hook = webhook_mod.get_webhook(session, grant.org_id, webhook_id)
    webhook_mod.delete_webhook(session, hook, actor=ctx.principal.actor, clock=state.clock)
    return ui.redirect("/portal/alerts", msg="Webhook deleted.")


# --- exports -------------------------------------------------------------------


@router.get("/exports", response_class=HTMLResponse)
def exports_page(request: Request, session: SessionDep, ctx: ui.OrgDep) -> HTMLResponse:
    grant = ui.require_grant(ctx)
    return ui.render(
        request,
        "portal/exports.html",
        ctx=ctx,
        jobs=exports.list_jobs(session, grant.org_id),
        types=[t.value for t in ExportType],
        formats=[f.value for f in ExportFormat],
        grant=grant,
    )


@router.post("/exports")
def export_create(
    state: StateDep,
    session: SessionDep,
    ctx: ui.OrgDep,
    _csrf: ui.CsrfDep,
    type: Annotated[str, Form()],
    format: Annotated[str, Form()] = "parquet",
    since: Annotated[str | None, Form()] = None,
    countries: Annotated[str | None, Form()] = None,
    platforms: Annotated[str | None, Form()] = None,
) -> Response:
    grant = ui.require_grant(ctx)
    spec = exports.ExportSpec(
        ExportType(type),
        ExportFormat(format),
        date.fromisoformat(since) if since else None,
        tuple(ui.parse_list(countries)),
        tuple(ui.parse_list(platforms)),
    )
    job = exports.request_export(
        session,
        grant=grant,
        principal=ctx.principal,
        spec=spec,
        settings=state.settings,
        clock=state.clock,
    )
    msg = (
        "Export requested; it exceeds your per-export limit and awaits compliance approval."
        if job.status == ExportStatus.AWAITING_APPROVAL
        else "Export requested; it will be built by the exporter shortly."
    )
    return ui.redirect("/portal/exports", msg=msg)


@router.get("/exports/{export_id}/download")
def export_download(
    state: StateDep, session: SessionDep, ctx: ui.OrgDep, export_id: uuid.UUID
) -> Response:
    if state.store is None:
        raise ConfigurationError("object store is not configured")
    job = exports.get_job(session, ctx.principal, export_id)
    url = exports.download(
        session,
        job,
        principal=ctx.principal,
        store=state.store,
        settings=state.settings,
        clock=state.clock,
    )
    return RedirectResponse(url, status_code=302)


# --- reports --------------------------------------------------------------------


@router.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request, session: SessionDep, ctx: ui.OrgDep) -> HTMLResponse:
    grant = ui.require_grant(ctx)
    return ui.render(
        request,
        "portal/reports.html",
        ctx=ctx,
        jobs=reports.recent(session, limit=50, org_id=grant.org_id),
    )


@router.get("/reports/{job_id}/{kind}")
def report_download(
    state: StateDep, session: SessionDep, ctx: ui.OrgDep, job_id: uuid.UUID, kind: str
) -> Response:
    grant = ui.require_grant(ctx)
    job = reports.get_job(session, job_id)
    if job.org_id != grant.org_id:
        raise NotFoundError("report not found")
    if state.store is None:
        raise ConfigurationError("object store is not configured")
    key, media, filename = reports.file_for(job, kind)
    data = state.store.get_bytes(state.settings.s3.bucket_exports, key)
    audit.record(
        session,
        actor=ctx.principal.actor,
        action="report.download",
        object_type="report_job",
        object_id=str(job.id),
        after={"kind": kind},
        ip=ctx.principal.ip,
        clock=state.clock,
    )
    return Response(
        data,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --- API keys -------------------------------------------------------------------


@router.get("/keys", response_class=HTMLResponse)
def keys_page(request: Request, session: SessionDep, ctx: ui.OrgDep) -> HTMLResponse:
    grant = ui.require_grant(ctx)
    rows = list(
        session.execute(
            select(ApiKey).where(ApiKey.org_id == grant.org_id).order_by(ApiKey.created_at.desc())
        ).scalars()
    )
    return ui.render(request, "portal/keys.html", ctx=ctx, keys=rows, scopes=sorted(SCOPES))


@router.post("/keys")
def key_issue(
    request: Request,
    state: StateDep,
    session: SessionDep,
    ctx: ui.OrgDep,
    _csrf: ui.CsrfDep,
    name: Annotated[str, Form()],
    scopes: Annotated[list[str], Form()] = [],  # noqa: B006 - form default
    expires_in_days: Annotated[str | None, Form()] = None,
    allowed_ips: Annotated[str | None, Form()] = None,
) -> Response:
    grant = ui.require_grant(ctx)
    require_role(ctx.principal, Role.ORG_ADMIN)
    issued = keys_mod.issue_key(
        session,
        org_id=grant.org_id,
        name=name.strip(),
        scopes=scopes,
        expires_in_days=ui.parse_int(expires_in_days),
        allowed_ips=ui.parse_list(allowed_ips),
        created_by=ctx.user.id,
        actor=ctx.principal.actor,
        pepper=state.pepper,
        prefix=state.settings.api.api_key_prefix,
        clock=state.clock,
    )
    return ui.render(
        request,
        "portal/secret_once.html",
        ctx=ctx,
        title="API key issued",
        label="API key (shown once)",
        secret=issued.raw,
        back="/portal/keys",
        note=f"Key {issued.key.prefix}… with scopes {', '.join(issued.key.scopes)}.",
    )


@router.post("/keys/{key_id}/revoke")
def key_revoke(
    state: StateDep, session: SessionDep, ctx: ui.OrgDep, _csrf: ui.CsrfDep, key_id: uuid.UUID
) -> Response:
    grant = ui.require_grant(ctx)
    require_role(ctx.principal, Role.ORG_ADMIN)
    key = session.get(ApiKey, key_id)
    if key is None or key.org_id != grant.org_id:
        raise NotFoundError("key not found")
    keys_mod.revoke_key(session, key, actor=ctx.principal.actor, clock=state.clock)
    return ui.redirect("/portal/keys", msg="Key revoked.")


# --- users ----------------------------------------------------------------------


@router.get("/users", response_class=HTMLResponse)
def users_page(request: Request, session: SessionDep, ctx: ui.OrgDep) -> HTMLResponse:
    grant = ui.require_grant(ctx)
    return ui.render(
        request,
        "portal/users.html",
        ctx=ctx,
        members=users_mod.users_of_org(session, grant.org_id),
        roles=sorted(r.value for r in ORG_ROLES - {Role.API_CLIENT}),
    )


@router.post("/users")
def user_create(
    state: StateDep,
    session: SessionDep,
    ctx: ui.OrgDep,
    _csrf: ui.CsrfDep,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    role: Annotated[str, Form()] = "org_viewer",
) -> Response:
    grant = ui.require_grant(ctx)
    require_role(ctx.principal, Role.ORG_ADMIN)
    r = Role(role)
    if r not in ORG_ROLES or r == Role.API_CLIENT:
        raise ValidationError("invalid organisation role")
    users_mod.create_user(
        session,
        email=email,
        password=password,
        role=r,
        org_id=grant.org_id,
        actor=ctx.principal.actor,
        clock=state.clock,
    )
    return ui.redirect("/portal/users", msg="User created; they must enrol 2FA at first login.")


@router.post("/users/{user_id}/role")
def user_role(
    state: StateDep,
    session: SessionDep,
    ctx: ui.OrgDep,
    _csrf: ui.CsrfDep,
    user_id: uuid.UUID,
    role: Annotated[str, Form()],
) -> Response:
    grant = ui.require_grant(ctx)
    require_role(ctx.principal, Role.ORG_ADMIN)
    r = Role(role)
    if r not in ORG_ROLES or r == Role.API_CLIENT:
        raise ValidationError("invalid organisation role")
    target = session.get(User, user_id)
    if target is None:
        raise NotFoundError("user not found")
    membership = users_mod.membership_of(session, target)
    if membership.org_id != grant.org_id:
        raise NotFoundError("user not found")
    users_mod.set_role(session, membership, r, actor=ctx.principal.actor, clock=state.clock)
    return ui.redirect("/portal/users", msg="Role updated.")


# --- usage ----------------------------------------------------------------------


@router.get("/usage", response_class=HTMLResponse)
def usage_page(
    request: Request, state: StateDep, session: SessionDep, ctx: ui.OrgDep
) -> HTMLResponse:
    grant = ui.require_grant(ctx)
    now = state.clock.now()
    rows = list(
        session.execute(
            select(UsageLog)
            .where(UsageLog.org_id == grant.org_id)
            .order_by(UsageLog.ts.desc())
            .limit(200)
        ).scalars()
    )
    per_endpoint = session.execute(
        select(UsageLog.endpoint, func.count(UsageLog.id), func.sum(UsageLog.records))
        .where(UsageLog.org_id == grant.org_id, UsageLog.ts >= _month_start(now))
        .group_by(UsageLog.endpoint)
        .order_by(func.count(UsageLog.id).desc())
    ).all()
    from payintel.abuse import usage_report as usage_mod

    reports = []
    for rep in usage_mod.reports_of(session, grant.org_id):
        link = None
        if rep.file_key and state.store is not None:
            link = state.store.presigned_get_url(
                state.settings.s3.bucket_exports, rep.file_key, expires_seconds=72 * 3600
            )
        reports.append((rep, link))
    return ui.render(
        request,
        "portal/usage.html",
        ctx=ctx,
        rows=rows,
        per_endpoint=per_endpoint,
        reports=reports,
        day=records_used(session, grant.org_id, since=_day_start(now)),
        month=records_used(session, grant.org_id, since=_month_start(now)),
        grant=grant,
    )


# --- API docs -------------------------------------------------------------------


@router.get("/docs", response_class=HTMLResponse)
def docs_page(request: Request, ctx: ui.UserDep) -> HTMLResponse:
    spec: dict[str, Any] = request.app.openapi()
    paths = []
    for path, ops in sorted(spec.get("paths", {}).items()):
        for method, op in ops.items():
            paths.append(
                {
                    "method": method.upper(),
                    "path": path,
                    "summary": op.get("summary") or op.get("operationId", ""),
                    "description": op.get("description") or "",
                    "params": [
                        f"{p['name']} ({p['in']}{', required' if p.get('required') else ''})"
                        for p in op.get("parameters", [])
                    ],
                    "tags": op.get("tags", []),
                }
            )
    schemas = sorted(spec.get("components", {}).get("schemas", {}).keys())
    return ui.render(request, "portal/docs.html", ctx=ctx, paths=paths, schemas=schemas)
