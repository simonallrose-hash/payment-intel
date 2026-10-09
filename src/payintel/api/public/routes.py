"""Public pages (no login): bot page, opt-out, GDPR requests (FR-OO-01…03, LR-06)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from starlette.responses import HTMLResponse, Response

from payintel.api import ui
from payintel.api.deps import SessionDep, StateDep
from payintel.compliance import bot_page, dsar, optout
from payintel.core.errors import ConfigurationError, NotFoundError
from payintel.core.models.base import DsarKind, OptoutMethod
from payintel.reports import public as public_mod
from payintel.reports import service as reports

router = APIRouter(tags=["public"], include_in_schema=False)

DSAR_DEADLINE_DAYS = 30


@router.get("/bot", response_class=HTMLResponse)
def bot(request: Request, state: StateDep) -> HTMLResponse:
    return render_bot(request, state, msg=None)


def render_bot(request: Request, state: StateDep, *, msg: str | None) -> HTMLResponse:
    page = bot_page.build(state.settings)
    return ui.render(request, "public/bot.html", page=page, msg=msg)


@router.get("/optout", response_class=HTMLResponse)
def optout_form(request: Request) -> HTMLResponse:
    return ui.render(request, "public/optout.html", methods=[m.value for m in OptoutMethod])


@router.post("/optout", response_class=HTMLResponse, dependencies=[Depends(ui.same_origin_guard)])
def optout_submit(
    request: Request,
    state: StateDep,
    session: SessionDep,
    domain: Annotated[str, Form()],
    method: Annotated[str, Form()] = "dns_txt",
    contact: Annotated[str | None, Form()] = None,
) -> HTMLResponse:
    req, instructions = optout.request(
        session,
        domain=domain,
        method=OptoutMethod(method),
        contact=(contact or "").strip() or None,
        clock=state.clock,
    )
    return ui.render(request, "public/optout_instructions.html", req=req, ins=instructions)


@router.get("/optout/verify/{token}", response_class=HTMLResponse)
def optout_status(request: Request, session: SessionDep, token: str) -> HTMLResponse:
    req = optout.get_by_token(session, token)
    return ui.render(
        request, "public/optout_instructions.html", req=req, ins=optout.instructions(req)
    )


@router.post(
    "/optout/verify/{token}",
    response_class=HTMLResponse,
    dependencies=[Depends(ui.same_origin_guard)],
)
def optout_verify(request: Request, state: StateDep, session: SessionDep, token: str) -> Response:
    req = optout.get_by_token(session, token)
    if state.dns_txt is None or state.http_get is None:
        raise ConfigurationError("opt-out verification resolvers are not configured")
    ok = optout.verify(
        session, req, dns_txt=state.dns_txt, http_get=state.http_get, clock=state.clock
    )
    if ok:
        optout.apply_verified(session, clock=state.clock)
    msg = (
        "Ownership confirmed: the domain is excluded from scanning and from client data."
        if ok
        else "Proof not found yet. Publish the record or file and try again."
    )
    return ui.render(
        request,
        "public/optout_instructions.html",
        req=req,
        ins=optout.instructions(req),
        msg=msg,
    )


@router.get("/dsar", response_class=HTMLResponse)
def dsar_form(request: Request) -> HTMLResponse:
    return ui.render(request, "public/dsar.html", kinds=[k.value for k in DsarKind])


@router.post("/dsar", response_class=HTMLResponse, dependencies=[Depends(ui.same_origin_guard)])
def dsar_submit(
    request: Request,
    state: StateDep,
    session: SessionDep,
    kind: Annotated[str, Form()],
    subject: Annotated[str, Form()],
    contact: Annotated[str, Form()],
    details: Annotated[str | None, Form()] = None,
) -> HTMLResponse:
    req = dsar.open_request(
        session,
        kind=DsarKind(kind),
        subject=subject,
        contact=contact,
        details=(details or "").strip() or None,
        deadline_days=DSAR_DEADLINE_DAYS,
        clock=state.clock,
    )
    return ui.render(request, "public/dsar_done.html", req=req)


# --- public report summaries (FR-RP-05) ------------------------------------------------------


@router.get("/reports", response_class=HTMLResponse)
def public_reports(request: Request, state: StateDep, session: SessionDep) -> HTMLResponse:
    jobs = reports.published(session)
    items = [
        (job, public_mod.PublicSummary.from_dict(job.public_summary))
        for job in jobs
        if job.public_summary
    ]
    return ui.render(request, "public/reports.html", items=items)


@router.get("/reports/{slug}.pdf")
def public_report_pdf(state: StateDep, session: SessionDep, slug: str) -> Response:
    job = reports.by_slug(session, slug)
    if state.store is None or not job.public_pdf_key:
        raise NotFoundError("summary PDF not available")
    data = state.store.get_bytes(state.settings.s3.bucket_exports, job.public_pdf_key)
    return Response(
        data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="payintel-summary-{slug}.pdf"'},
    )


@router.get("/reports/{slug}", response_class=HTMLResponse)
def public_report(
    request: Request, state: StateDep, session: SessionDep, slug: str
) -> HTMLResponse:
    job = reports.by_slug(session, slug)
    summary = public_mod.PublicSummary.from_dict(job.public_summary or {})
    return ui.render(
        request,
        "public/report.html",
        job=job,
        s=summary,
        methodology_url=state.settings.api.methodology_url,
    )
