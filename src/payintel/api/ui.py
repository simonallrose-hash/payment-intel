"""Shared pieces of the server-rendered portal and admin (FR-UI-*, FR-ADM-*).

* Jinja2 templates under `api/templates`, autoescaped; no inline scripts or
  styles, so the CSP in `app.py` can forbid `unsafe-inline` (NFR-S-05).
* A `UiContext` per request: the portal session row, user, membership and the
  `Principal`; org users also carry their `Grant` (or the denial reason).
* `LoginRequired` is turned into a redirect to the login page by `problems`.
* CSRF: every state-changing form carries the session's token (`csrf_guard`).
* Login/TOTP forms have no session yet; `same_origin_guard` checks `Origin`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote, urlencode, urlsplit

from fastapi import Depends, Form, Request
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.orm import Session
from starlette.responses import HTMLResponse, RedirectResponse, Response

from payintel.api.auth import csrf as csrf_mod
from payintel.api.auth import sessions as sessions_mod
from payintel.api.deps import AppState, SessionDep, StateDep, client_ip
from payintel.compliance import users as users_mod
from payintel.core.errors import ForbiddenError
from payintel.core.flags import FlagService
from payintel.core.models.access import Membership, User
from payintel.core.models.base import Role
from payintel.core.models.portal import PortalSession
from payintel.entitlements.check import EntitlementDenied, require_staff, resolve_grant
from payintel.entitlements.model import DenialCode, Grant, Principal

TEMPLATES_DIR = Path(__file__).with_name("templates")

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def _fmt_dt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M UTC")
    return str(value)


def _fmt_pct(value: Any) -> str:
    return "—" if value is None else f"{float(value) * 100:.1f} %"


_env.filters["dt"] = _fmt_dt
_env.filters["pct"] = _fmt_pct

UI_PREFIXES: tuple[str, ...] = ("/portal", "/admin", "/bot", "/optout", "/dsar")


def is_ui_path(path: str) -> bool:
    return path.startswith(UI_PREFIXES)


class LoginRequired(Exception):
    """Raised by UI dependencies; rendered as a 303 to the login page."""

    def __init__(self, next_url: str, *, step: str = "login") -> None:
        super().__init__(step)
        self.next_url = next_url
        self.step = step


@dataclass
class UiContext:
    session_row: PortalSession
    user: User
    membership: Membership
    principal: Principal
    grant: Grant | None = None
    denial: str | None = None

    @property
    def csrf(self) -> str:
        return self.session_row.csrf_token

    @property
    def is_staff(self) -> bool:
        return self.principal.is_staff

    @property
    def org_id(self) -> uuid.UUID | None:
        return self.membership.org_id


def render(
    request: Request, name: str, *, status: int = 200, ctx: UiContext | None = None, **values: Any
) -> HTMLResponse:
    state: AppState = request.app.state.payintel
    template = _env.get_template(name)
    values.setdefault("msg", request.query_params.get("msg"))
    html = template.render(
        request=request,
        ui=ctx,
        identity=state.settings.identity,
        now=state.clock.now(),
        **values,
    )
    return HTMLResponse(html, status_code=status)


def redirect(url: str, *, msg: str | None = None) -> RedirectResponse:
    if msg:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{urlencode({'msg': msg})}"
    return RedirectResponse(url, status_code=303)


def html_error(request: Request, status: int, title: str, detail: str | None) -> HTMLResponse:
    ctx: UiContext | None = getattr(request.state, "ui", None)
    return render(
        request, "error.html", status=status, ctx=ctx, title=title, detail=detail, code=status
    )


def set_session_cookie(response: Response, token: str, state: AppState) -> None:
    response.set_cookie(
        sessions_mod.COOKIE_NAME,
        token,
        max_age=state.settings.api.session_idle_hours * 3600,
        httponly=True,
        secure=state.settings.api.cookie_secure,
        samesite="strict",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(sessions_mod.COOKIE_NAME, path="/")


def same_origin_guard(request: Request) -> None:
    """For forms posted before a session exists (login, TOTP): the `Origin` (or
    `Referer`) host must be our own host. Browsers always send one of them on
    cross-site POSTs; a missing header is accepted for non-browser clients."""
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        return
    host = urlsplit(origin).hostname
    own = request.url.hostname
    if host is None or own is None or host.lower() != own.lower():
        raise csrf_mod.CsrfError("cross-origin form submission rejected")


def load_context(request: Request, state: AppState, session: Session) -> UiContext | None:
    token = request.cookies.get(sessions_mod.COOKIE_NAME)
    row = sessions_mod.load_session(
        session, token, now=state.clock.now(), idle_hours=state.settings.api.session_idle_hours
    )
    if row is None:
        return None
    user = session.get(User, row.user_id)
    if user is None:
        return None
    membership = users_mod.membership_of(session, user)
    principal = users_mod.principal_for(user, membership, ip=client_ip(request))
    ctx = UiContext(session_row=row, user=user, membership=membership, principal=principal)
    if membership.org_id is not None:
        try:
            flags = FlagService(session, state.settings.flags, clock=state.clock)
            ctx.grant = resolve_grant(
                session, membership.org_id, today=state.clock.now().date(), flags=flags
            )
        except EntitlementDenied as exc:
            ctx.denial = exc.reason
    request.state.ui = ctx
    request.state.principal = principal
    return ctx


def _next_url(request: Request) -> str:
    return quote(str(request.url.path), safe="/")


def current_user(request: Request, state: StateDep, session: SessionDep) -> UiContext:
    """A fully signed-in user (password + TOTP). Otherwise: redirect to the login step."""
    ctx = load_context(request, state, session)
    if ctx is None:
        raise LoginRequired(_next_url(request))
    if not ctx.session_row.totp_verified:
        raise LoginRequired(_next_url(request), step="totp")
    return ctx


def pending_user(request: Request, state: StateDep, session: SessionDep) -> UiContext:
    """A user past the password step (TOTP enrolment / verification pages)."""
    ctx = load_context(request, state, session)
    if ctx is None:
        raise LoginRequired(_next_url(request))
    return ctx


UserDep = Annotated[UiContext, Depends(current_user)]
PendingDep = Annotated[UiContext, Depends(pending_user)]


def org_user(ctx: UserDep) -> UiContext:
    """Portal pages: an organisation member with a live entitlement."""
    if ctx.is_staff:
        return ctx
    if ctx.grant is None:
        raise EntitlementDenied(
            ctx.denial or DenialCode.NO_ENTITLEMENT,
            "your organisation has no active entitlement",
        )
    return ctx


OrgDep = Annotated[UiContext, Depends(org_user)]


def staff_user(ctx: UserDep) -> UiContext:
    if not ctx.is_staff:
        raise ForbiddenError("staff only")
    return ctx


StaffDep = Annotated[UiContext, Depends(staff_user)]


def staff_roles(*roles: Role) -> Any:
    def dep(ctx: StaffDep) -> UiContext:
        require_staff(ctx.principal, *roles)
        return ctx

    return dep


def csrf_guard(ctx: PendingDep, csrf: Annotated[str | None, Form()] = None) -> None:
    csrf_mod.check(ctx.session_row.csrf_token, csrf)


CsrfDep = Annotated[None, Depends(csrf_guard)]


def require_grant(ctx: UiContext) -> Grant:
    if ctx.grant is None:
        raise EntitlementDenied(ctx.denial or DenialCode.NO_ENTITLEMENT, "no entitlement")
    return ctx.grant


def parse_list(raw: str | None) -> list[str]:
    """`"DE, AT\\nCH"` → `["DE", "AT", "CH"]`."""
    if not raw:
        return []
    out: list[str] = []
    for chunk in raw.replace("\n", ",").replace(";", ",").split(","):
        v = chunk.strip()
        if v:
            out.append(v)
    return out


def parse_int(raw: str | None, *, default: int | None = None) -> int | None:
    if raw is None or not raw.strip():
        return default
    return int(raw.strip())
