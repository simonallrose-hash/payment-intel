"""RFC 9457 problem responses for every error the API raises (docs/api.md)."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse, RedirectResponse, Response

from payintel.api import ui
from payintel.api.auth.csrf import CsrfError
from payintel.core.errors import (
    C2DisabledError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    PayIntelError,
    UpstreamError,
    ValidationError,
)
from payintel.entitlements.check import EntitlementDenied
from payintel.entitlements.quotas import QuotaExceeded, RateLimited

PROBLEM_TYPE = "application/problem+json"

_STATUS: dict[type[PayIntelError], int] = {
    ValidationError: 400,
    NotFoundError: 404,
    ConflictError: 409,
    ForbiddenError: 403,
    EntitlementDenied: 403,
    CsrfError: 403,
    C2DisabledError: 403,
    RateLimited: 429,
    QuotaExceeded: 429,
    UpstreamError: 503,
}

_TITLE: dict[int, str] = {
    400: "Bad Request",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    429: "Too Many Requests",
    500: "Internal Server Error",
    503: "Service Unavailable",
}


def status_for(exc: PayIntelError) -> int:
    for cls in type(exc).__mro__:
        if cls in _STATUS:
            return _STATUS[cls]
    return 500


def problem(
    status: int,
    code: str,
    detail: str | None,
    *,
    reason: str | None = None,
    instance: str | None = None,
    headers: dict[str, str] | None = None,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": "about:blank",
        "title": _TITLE.get(status, "Error"),
        "status": status,
        "code": code,
        "detail": detail,
        "reason": reason,
        "instance": instance,
    }
    if extra:
        body.update(extra)
    return JSONResponse(body, status_code=status, media_type=PROBLEM_TYPE, headers=headers)


def install(app: FastAPI) -> None:
    @app.exception_handler(ui.LoginRequired)
    async def _login(request: Request, exc: ui.LoginRequired) -> Response:
        target = "/portal/login" if exc.step == "login" else "/portal/login/totp"
        return RedirectResponse(f"{target}?next={exc.next_url}", status_code=303)

    @app.exception_handler(PayIntelError)
    async def _payintel(request: Request, exc: PayIntelError) -> Response:
        status = status_for(exc)
        headers: dict[str, str] = {}
        if isinstance(exc, RateLimited | QuotaExceeded):
            headers["Retry-After"] = str(exc.retry_after_seconds)
        reason = getattr(exc, "reason", None)
        if reason is not None:
            request.state.denial = str(reason)  # journalled for the abuse detectors (FR-AB-02)
        detail = exc.message if status < 500 else "internal error"
        if ui.is_ui_path(request.url.path):
            title = _TITLE.get(status, "Error")
            text = detail if reason is None else f"{detail} ({reason})"
            return ui.html_error(request, status, title, text)
        return problem(
            status,
            exc.code,
            detail,
            reason=reason,
            instance=str(request.url.path),
            headers=headers or None,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> Response:
        """Framework errors (unknown route, unparsable body, 405): same problem shape."""
        detail = str(exc.detail) if exc.detail else _TITLE.get(exc.status_code, "Error")
        if ui.is_ui_path(request.url.path):
            return ui.html_error(
                request, exc.status_code, _TITLE.get(exc.status_code, "Error"), detail
            )
        code = {
            400: "validation_error",
            404: "not_found",
            405: "method_not_allowed",
            413: "too_large",
            415: "unsupported_media_type",
        }.get(exc.status_code, "http_error")
        return problem(
            exc.status_code,
            code,
            detail,
            instance=str(request.url.path),
            headers=dict(exc.headers) if exc.headers else None,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> Response:
        if ui.is_ui_path(request.url.path):
            msgs = "; ".join(
                f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()
            )
            return ui.html_error(request, 400, "Bad Request", msgs)
        return problem(
            400,
            "validation_error",
            "request validation failed",
            instance=str(request.url.path),
            extra={"errors": [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]},
        )
