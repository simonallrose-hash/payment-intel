"""FastAPI application factory.

* `/v1/*`: client API behind API keys and entitlements (FR-API-*);
* `/portal/*`, `/admin/*`: server-rendered pages behind password + TOTP sessions;
* `/bot`, `/optout/*`: public pages (FR-OO-01/02);
* `/healthz`, `/openapi.json`.

Middleware: request id (NFR-M-05), security headers (NFR-S-05: CSP without
`unsafe-inline`), usage journal for every /v1 request (FR-API-09).
"""

from __future__ import annotations

import hmac
import time
import uuid
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.openapi.utils import get_openapi
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.routing import Match

from payintel.api import problems
from payintel.api.deps import AppState, client_ip
from payintel.api.schemas.common import Problem
from payintel.api.usage import log_usage
from payintel.api.v1 import changes, exports, reference, stats, stores, usage, watchlists, webhooks
from payintel.core import metrics
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


# Documented for every /v1 operation (FR-API-08): the API answers 4xx only
# with RFC 9457 problem documents (`application/problem+json`), see docs/api.md.
PROBLEM_RESPONSES: dict[str, str] = {
    "400": "Validation error (`code=validation_error`, `errors[]`)",
    "403": "Entitlement, scope, segment, role or key denied (`code`, `reason`)",
    "404": "Not found or not visible to the caller",
    "429": "Rate limit or record quota exceeded (`Retry-After`)",
}


def _openapi_with_problems(app: FastAPI) -> Callable[[], dict[str, Any]]:
    def build() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        doc = get_openapi(
            title=app.title,
            version=app.version,
            openapi_version=app.openapi_version,
            description=app.description,
            routes=app.routes,
        )
        schemas = doc.setdefault("components", {}).setdefault("schemas", {})
        problem = Problem.model_json_schema(ref_template="#/components/schemas/{model}")
        schemas.update(problem.pop("$defs", {}))
        schemas["Problem"] = problem
        ref = {"schema": {"$ref": "#/components/schemas/Problem"}}
        for path, ops in doc["paths"].items():
            if not path.startswith("/v1/"):
                continue
            for op in ops.values():
                responses = op.setdefault("responses", {})
                responses.pop("422", None)  # validation errors are 400 problems
                for status, description in PROBLEM_RESPONSES.items():
                    responses[status] = {
                        "description": description,
                        "content": {problems.PROBLEM_TYPE: ref},
                    }
        app.openapi_schema = doc
        return doc

    return build


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
    app.openapi = _openapi_with_problems(app)  # type: ignore[method-assign]
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

    @app.get("/metrics", include_in_schema=False)
    def metrics_endpoint(request: Request) -> Response:
        """Prometheus exposition (NFR-P-07), only for the configured scrape token."""
        token = state.settings.secrets.metrics_token.get_secret_value()
        presented = request.headers.get("authorization", "")
        if not token or not hmac.compare_digest(presented, f"Bearer {token}"):
            return Response(status_code=404)
        body, content_type = metrics.render()
        return Response(content=body, media_type=content_type)

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
        elapsed = time.perf_counter() - started
        duration_ms = int(elapsed * 1000)
        route = request.scope.get("route")
        if request.url.path.startswith("/v1/") and route is not None:
            metrics.API_REQUEST_SECONDS.labels(
                getattr(route, "path", request.url.path), request.method, str(response.status_code)
            ).observe(elapsed)
        if response.status_code == 405:
            response.headers["Allow"] = ", ".join(_allowed_methods(app, request.scope))
        response.headers["X-Request-Id"] = request_id
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        if request.url.path.startswith("/v1/"):
            await run_in_threadpool(
                _journal, request, state, duration_ms, request_id, response.status_code
            )
        return response

    return app


def _allowed_methods(app: FastAPI, scope: MutableMapping[str, Any]) -> list[str]:
    """Every method any route accepts for this path; Starlette's own 405 only
    names the first partially matching route."""
    probe = dict(scope)
    probe["method"] = "OPTIONS"
    methods: set[str] = set()
    stack: list[Any] = list(app.routes)
    while stack:
        route = stack.pop()
        inner = getattr(route, "original_router", None)
        if inner is not None:
            stack.extend(inner.routes)
            continue
        route_methods = getattr(route, "methods", None)
        if not route_methods:
            continue
        match, _ = route.matches(probe)
        if match != Match.NONE:
            methods |= set(route_methods)
    return sorted(methods - {"HEAD"} | ({"HEAD"} if "GET" in methods else set()))


def _journal(
    request: Request, state: AppState, duration_ms: int, request_id: str, status: int
) -> None:
    principal = getattr(request.state, "principal", None)
    if principal is None or principal.org_id is None:
        return
    params = {k: v for k, v in request.query_params.multi_items()}
    params["status"] = str(status)
    denial = getattr(request.state, "denial", None)
    if denial:
        params["denial"] = str(denial)
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
