"""FastAPI application factory.

* `/v1/*`: client API behind API keys and entitlements (FR-API-*);
* `/portal/*`, `/admin/*`: server-rendered pages behind password + TOTP sessions;
* `/bot`, `/optout/*`: public pages (FR-OO-01/02);
* `/healthz`, `/openapi.json`.

Middleware: request id (NFR-M-05), security headers (NFR-S-05: CSP without
`unsafe-inline`), usage journal for every /v1 request (FR-API-09).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles

from payintel.api import problems
from payintel.api.deps import AppState, client_ip
from payintel.api.usage import log_usage
from payintel.api.v1 import changes, exports, reference, stats, stores, usage, watchlists, webhooks
from payintel.core.logging import get_logger, log_context

log = get_logger(__name__)

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data: https:; "
    "font-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; "
    "base-uri 'none'"
)

STATIC_DIR = __import__("pathlib").Path(__file__).with_name("static")


def _usage_endpoint(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", request.url.path)
    return f"{request.method} {path}"


def create_app(state: AppState) -> FastAPI:
    app = FastAPI(
        title="PayIntel API",
        version="1.0",
        openapi_version="3.1.0",
        docs_url=None,
        redoc_url=None,
        description="Module C — payment infrastructure of e-commerce sites. "
        "Bearer API keys, entitlements per organisation (FR-API-*).",
    )
    app.state.payintel = state
    problems.install(app)
    for router in (
        stores.router,
        reference.router,
        stats.router,
        changes.router,
        watchlists.router,
        webhooks.router,
        exports.router,
        usage.router,
    ):
        app.include_router(router)
    from payintel.api.admin import routes as admin_routes
    from payintel.api.portal import routes as portal_routes
    from payintel.api.public import router as public_router

    app.include_router(public_router)
    app.include_router(portal_routes.router)
    app.include_router(admin_routes.router)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.middleware("http")
    async def _request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = request_id
        request.state.records = 0
        started = time.perf_counter()
        with log_context(request_id=request_id):
            response = await call_next(request)
        duration_ms = int((time.perf_counter() - started) * 1000)
        response.headers["X-Request-Id"] = request_id
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        if request.url.path.startswith("/v1/"):
            _journal(request, state, duration_ms, request_id, response.status_code)
        return response

    return app


def _journal(
    request: Request, state: AppState, duration_ms: int, request_id: str, status: int
) -> None:
    principal = getattr(request.state, "principal", None)
    if principal is None or principal.org_id is None:
        return
    params = {k: v for k, v in request.query_params.multi_items()}
    params["status"] = str(status)
    session = state.session_factory()
    try:
        log_usage(
            session,
            org_id=principal.org_id,
            endpoint=_usage_endpoint(request),
            params=params,
            records=int(getattr(request.state, "records", 0) or 0),
            ip=client_ip(request),
            ts=state.clock.now(),
            duration_ms=duration_ms,
            api_key_id=principal.api_key_id,
            user_id=principal.user_id,
            request_id=request_id,
        )
        session.commit()
    except Exception:
        session.rollback()
        log.exception("usage journal failed")
    finally:
        session.close()
