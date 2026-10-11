"""AC-07 and FR-API-02/05/06/07/08/09/10 over the real application.

The 403 matrix: endpoint × (missing key, revoked, expired, wrong IP, missing
scope, outside segment, suspended organisation, contract out of term, no
entitlement, other organisation's objects) → `application/problem+json` with
a stable `reason`. Every C1 payload is scanned for AS-23 keys.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from payintel.api.auth import keys as keys_mod
from payintel.api.deps import AppState
from payintel.compliance import contracts as contracts_mod
from payintel.compliance import lifecycle
from payintel.core.clock import FixedClock
from payintel.core.models.audit import UsageLog
from payintel.core.models.base import OrgStatus
from payintel.core.models.orgs import Entitlement
from payintel.entitlements.profiles import FORBIDDEN_C1_KEYS
from tests.stage3.conftest import World, auth, issue_key, login, staff_principal

pytestmark = pytest.mark.integration

READ_ENDPOINTS = [
    "/v1/stores/alpha-shop.de",
    "/v1/stores/alpha-shop.de/history",
    "/v1/stores?country=DE",
    "/v1/providers",
    "/v1/payment-methods",
    "/v1/stats/market-share?country=DE",
    "/v1/changes",
    "/v1/watchlists",
    "/v1/webhooks",
    "/v1/exports",
    "/v1/usage",
]


def _keys(obj: Any, acc: set[str]) -> set[str]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            acc.add(k)
            _keys(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            _keys(v, acc)
    return acc


def _problem(r: Any, status: int, reason: str | None = None) -> None:
    assert r.status_code == status, r.text
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["status"] == status
    if reason is not None:
        assert body["reason"] == reason, body


# --- authentication ---------------------------------------------------------------------


def test_missing_and_malformed_keys(client: TestClient, world: World) -> None:
    _problem(client.get("/v1/stores/alpha-shop.de"), 403, "key_revoked")
    _problem(
        client.get("/v1/stores/alpha-shop.de", headers=auth("pik_zz.nope")), 403, "key_revoked"
    )
    bad = world.api_key[:-4] + "aaaa"
    _problem(client.get("/v1/stores/alpha-shop.de", headers=auth(bad)), 403, "key_revoked")


def test_key_shown_once_and_only_hash_stored(db_session: Session, world: World) -> None:
    from payintel.core.models.access import ApiKey

    row = db_session.get(ApiKey, world.api_key_id)
    assert row is not None
    assert world.api_key.startswith(row.prefix + ".")
    assert row.hash != world.api_key and len(row.hash) == 64
    assert world.api_key not in (row.hash, row.prefix)


def test_revoked_and_expired_keys(
    client: TestClient,
    db_session: Session,
    world: World,
    app_state: AppState,
    fixed_clock: FixedClock,
) -> None:
    short = issue_key(
        db_session, world.org, clock=fixed_clock, settings=app_state.settings, expires_in_days=1
    )
    assert client.get("/v1/usage", headers=auth(short.raw)).status_code == 200
    fixed_clock.advance(days=2)
    _problem(client.get("/v1/usage", headers=auth(short.raw)), 403, "key_expired")
    fixed_clock.advance(days=-2)
    keys_mod.revoke_key(db_session, short.key, actor="test", clock=fixed_clock)
    _problem(client.get("/v1/usage", headers=auth(short.raw)), 403, "key_revoked")


def test_ip_allow_list(
    client: TestClient,
    db_session: Session,
    world: World,
    app_state: AppState,
    fixed_clock: FixedClock,
) -> None:
    key = issue_key(
        db_session,
        world.org,
        clock=fixed_clock,
        settings=app_state.settings,
        allowed_ips=["203.0.113.0/24"],
    )
    _problem(client.get("/v1/usage", headers=auth(key.raw)), 403, "ip_not_allowed")
    ok = client.get(
        "/v1/usage", headers={**auth(key.raw), "X-Forwarded-For": "203.0.113.7, 10.0.0.1"}
    )
    assert ok.status_code == 200, ok.text


def test_scope_missing(
    client: TestClient,
    db_session: Session,
    world: World,
    app_state: AppState,
    fixed_clock: FixedClock,
) -> None:
    key = issue_key(
        db_session, world.org, clock=fixed_clock, settings=app_state.settings, scopes=["usage:read"]
    )
    assert client.get("/v1/usage", headers=auth(key.raw)).status_code == 200
    for path in ("/v1/stores/alpha-shop.de", "/v1/changes", "/v1/stats/market-share?country=DE"):
        _problem(client.get(path, headers=auth(key.raw)), 403, "scope_missing")
    _problem(
        client.post("/v1/watchlists", json={"name": "x"}, headers=auth(key.raw)),
        403,
        "scope_missing",
    )


# --- entitlements ------------------------------------------------------------------------


@pytest.mark.parametrize("path", READ_ENDPOINTS)
def test_every_endpoint_requires_an_active_organisation(
    client: TestClient, db_session: Session, world: World, fixed_clock: FixedClock, path: str
) -> None:
    assert client.get(path, headers=auth(world.api_key)).status_code == 200, path
    lifecycle.transition(
        db_session,
        world.org,
        OrgStatus.SUSPENDED,
        principal=staff_principal(),
        comment="payment overdue",
        clock=fixed_clock,
    )
    _problem(client.get(path, headers=auth(world.api_key)), 403, "org_not_active")


def test_contract_out_of_term_and_no_entitlement(
    client: TestClient, db_session: Session, world: World, fixed_clock: FixedClock
) -> None:
    headers = auth(world.api_key)
    fixed_clock.advance(days=400)
    _problem(client.get("/v1/usage", headers=headers), 403, "contract_not_in_term")
    fixed_clock.advance(days=-400)
    ent = db_session.execute(
        select(Entitlement).where(Entitlement.contract_id == world.contract.id)
    ).scalar_one()
    contracts_mod.deactivate_entitlement(
        db_session, ent, principal=staff_principal(), clock=fixed_clock
    )
    _problem(client.get("/v1/usage", headers=headers), 403, "no_entitlement")


def test_outside_segment_is_403_with_reason(client: TestClient, world: World) -> None:
    headers = auth(world.api_key)
    _problem(client.get("/v1/stores/delta-boutique.fr", headers=headers), 403, "outside_segment")
    _problem(client.get("/v1/stores?country=FR", headers=headers), 403, "outside_segment")
    _problem(client.get("/v1/changes?country=FR", headers=headers), 403, "outside_segment")
    _problem(
        client.get("/v1/stats/market-share?country=FR", headers=headers), 403, "outside_segment"
    )
    page = client.get("/v1/stores", headers=headers).json()
    assert {s["domain"] for s in page["items"]} == {
        "alpha-shop.de",
        "beta-store.de",
        "gamma-market.de",
    }


def test_lineage_and_optout_never_leak(client: TestClient, world: World) -> None:
    headers = auth(world.api_key)
    assert client.get("/v1/stores/czds-only.de", headers=headers).status_code == 404
    assert client.get("/v1/stores/optedout.de", headers=headers).status_code == 404
    assert client.get("/v1/stores/not-a-shop.de", headers=headers).status_code == 404
    domains = {s["domain"] for s in client.get("/v1/stores", headers=headers).json()["items"]}
    assert not domains & {"czds-only.de", "optedout.de", "not-a-shop.de"}
    changes = client.get("/v1/changes", headers=headers).json()["items"]
    assert not {c["domain"] for c in changes} & {"czds-only.de", "optedout.de"}


def test_other_organisations_objects_are_invisible(client: TestClient, world: World) -> None:
    mine = client.post(
        "/v1/watchlists",
        json={"name": "w", "domains": ["alpha-shop.de"]},
        headers=auth(world.api_key),
    )
    assert mine.status_code == 201, mine.text
    wid = mine.json()["id"]
    assert client.get(f"/v1/watchlists/{wid}", headers=auth(world.api_key)).status_code == 200
    assert client.get(f"/v1/watchlists/{wid}", headers=auth(world.other_api_key)).status_code == 404


# --- AS-23 / FR-API-08 -------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/v1/stores/alpha-shop.de",
        "/v1/stores/alpha-shop.de/history",
        "/v1/stores",
        "/v1/changes",
        "/v1/stats/market-share?country=DE",
    ],
)
def test_no_forbidden_keys_in_c1_full(client: TestClient, world: World, path: str) -> None:
    body = client.get(path, headers=auth(world.api_key)).json()
    keys = _keys(body, set())
    assert not keys & FORBIDDEN_C1_KEYS, keys & FORBIDDEN_C1_KEYS
    assert "9.1.2" not in str(body)  # platform version never serialised
    assert "google-analytics.com" not in str(body)  # non-PSP third party host


def test_no_forbidden_keys_in_c1_basic(client: TestClient, world: World) -> None:
    body = client.get("/v1/stores/delta-boutique.fr", headers=auth(world.other_api_key)).json()
    assert not _keys(body, set()) & FORBIDDEN_C1_KEYS
    assert "evidence" not in str(body) and "checkout_psp_hosts" not in body
    assert body["providers"][0]["id"] == "mollie"


def test_full_profile_has_evidence_fields_and_psp_hosts(client: TestClient, world: World) -> None:
    body = client.get("/v1/stores/alpha-shop.de", headers=auth(world.api_key)).json()
    assert body["checkout_psp_hosts"] == ["adyen.com"]
    p = body["providers"][0]
    assert {"id", "name", "role", "confidence", "first_seen", "last_seen", "evidence"} <= set(p)
    assert body["as_of"] and body["confidence"] and body["coverage"] and body["methodology_url"]


# --- pagination, sorting, filters ------------------------------------------------------------


def test_cursor_pagination_and_sorting(client: TestClient, world: World) -> None:
    headers = auth(world.api_key)
    first = client.get("/v1/stores?limit=2&sort=traffic_rank", headers=headers).json()
    assert [s["domain"] for s in first["items"]] == ["beta-store.de", "alpha-shop.de"]
    assert first["next_cursor"]
    second = client.get(
        f"/v1/stores?limit=2&sort=traffic_rank&cursor={first['next_cursor']}", headers=headers
    ).json()
    assert [s["domain"] for s in second["items"]] == ["gamma-market.de"]
    assert second["next_cursor"] is None
    assert client.get("/v1/stores?limit=5000", headers=headers).status_code == 400


def test_filters(client: TestClient, world: World) -> None:
    headers = auth(world.api_key)

    def domains(q: str) -> set[str]:
        r = client.get(f"/v1/stores?{q}", headers=headers)
        assert r.status_code == 200, (q, r.text)
        return {s["domain"] for s in r.json()["items"]}

    assert domains("provider=stripe") == {"beta-store.de"}
    assert domains("without_provider=adyen") == {"beta-store.de", "gamma-market.de"}
    assert domains("provider_role=bnpl") == {"beta-store.de"}
    assert domains("payment_method=klarna") == {"beta-store.de"}
    assert domains("providers_min=2") == {"beta-store.de"}
    assert domains("platform=woocommerce") == {"beta-store.de"}
    assert domains("domain_prefix=gam") == {"gamma-market.de"}
    assert domains("changed_from=2026-10-01T00:00:00Z") == {
        "alpha-shop.de",
        "beta-store.de",
        "gamma-market.de",
    }
    assert domains("changed_from=2026-10-07T00:00:00Z") == set()


def test_filters_by_confidence_and_dates(client: TestClient, world: World) -> None:
    """FR-API-04: the remaining filters (providers first_seen 2026-09-08, last_seen 2026-10-06).
    A separate test because the fixed clock keeps every call in one rate-limit window."""
    headers = auth(world.api_key)

    def domains(q: str) -> set[str]:
        r = client.get(f"/v1/stores?{q}", headers=headers)
        assert r.status_code == 200, (q, r.text)
        return {s["domain"] for s in r.json()["items"]}

    assert domains("providers_max=1") == {"alpha-shop.de", "gamma-market.de"}
    assert domains("min_confidence=high") == {"alpha-shop.de", "beta-store.de", "gamma-market.de"}
    assert domains("first_seen_from=2026-09-08") == {
        "alpha-shop.de",
        "beta-store.de",
        "gamma-market.de",
    }
    assert domains("first_seen_from=2026-09-09") == set()
    assert domains("first_seen_to=2026-09-07") == set()
    assert domains("last_seen_from=2026-10-06&last_seen_to=2026-10-06") == {
        "alpha-shop.de",
        "beta-store.de",
        "gamma-market.de",
    }
    assert domains("last_seen_to=2026-10-05") == set()
    r = client.get("/v1/stores?min_confidence=certain", headers=headers)
    assert r.status_code == 400


# --- rate limit, quota, usage journal ------------------------------------------------------


def test_rate_limit_429_with_retry_after(client: TestClient, world: World) -> None:
    headers = auth(world.api_key)
    statuses = [client.get("/v1/usage", headers=headers).status_code for _ in range(12)]
    assert statuses[:10] == [200] * 10 and statuses[10] == 429
    r = client.get("/v1/usage", headers=headers)
    assert r.status_code == 429 and r.headers["Retry-After"].isdigit()
    assert r.json()["code"] == "rate_limited"


def test_daily_quota(
    client: TestClient,
    db_session: Session,
    world: World,
    app_state: AppState,
    fixed_clock: FixedClock,
) -> None:
    ent = db_session.execute(
        select(Entitlement).where(Entitlement.contract_id == world.contract.id)
    ).scalar_one()
    ent.daily_records = 2
    db_session.flush()
    headers = auth(world.api_key)
    assert client.get("/v1/stores", headers=headers).status_code == 200  # 3 records journaled
    r = client.get("/v1/stores", headers=headers)
    assert r.status_code == 429 and r.json()["code"] == "quota_exceeded"
    assert int(r.headers["Retry-After"]) > 0


def test_usage_journal_rows(client: TestClient, db_session: Session, world: World) -> None:
    fwd = {**auth(world.api_key), "X-Forwarded-For": "198.51.100.9"}
    client.get("/v1/stores?country=DE&limit=2", headers=fwd)
    client.get("/v1/stores/nope.de", headers=fwd)
    rows = list(
        db_session.execute(
            select(UsageLog).where(UsageLog.org_id == world.org.id).order_by(UsageLog.id)
        ).scalars()
    )
    assert [r.endpoint for r in rows][-2:] == ["GET /v1/stores", "GET /v1/stores/{domain}"]
    hit = rows[-2]
    assert hit.records == 2 and hit.api_key_id == world.api_key_id and hit.duration_ms >= 0
    assert hit.params["country"] == "DE" and hit.params["status"] == "200"
    assert str(hit.ip) == "198.51.100.9"
    assert rows[-1].params["status"] == "404"
    usage = client.get("/v1/usage", headers=auth(world.api_key)).json()
    assert usage["day_records"] == 2 and usage["daily_limit"] == 50_000
    assert db_session.execute(select(func.count(UsageLog.id))).scalar_one() >= 3


def test_metrics_endpoint_needs_scrape_token_and_counts_requests(
    client: TestClient, world: World, app_state: AppState
) -> None:
    """NFR-P-07: Prometheus exposition behind `secrets.metrics_token`; API latency histogram."""
    from pydantic import SecretStr

    assert client.get("/metrics").status_code == 404  # no token configured → not there
    app_state.settings.secrets.metrics_token = SecretStr("scrape-me")
    assert client.get("/metrics").status_code == 404
    assert client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 404
    client.get("/v1/stores/alpha-shop.de", headers=auth(world.api_key))
    r = client.get("/metrics", headers={"Authorization": "Bearer scrape-me"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    body = r.text
    assert 'payintel_api_request_seconds_count{method="GET",route="/v1/stores/{domain}"' in body
    assert "payintel_alert_latency_seconds" in body and "payintel_alert_backlog_age_seconds" in body
    assert "pik_" not in body  # never a key or a domain in the exposition


def test_every_request_logs_request_id_and_org_id(client: TestClient, world: World) -> None:
    """NFR-M-05: one structured line per request carries the organisation id."""
    import structlog.testing

    with structlog.testing.capture_logs() as records:
        r = client.get("/v1/stores/alpha-shop.de", headers=auth(world.api_key))
    assert r.status_code == 200
    line = next(rec for rec in records if rec["event"] == "request")
    assert line["org_id"] == str(world.org.id) and line["route"] == "/v1/stores/{domain}"
    assert line["status"] == 200 and r.headers["X-Request-Id"]


def test_api_keeps_serving_profiles_when_clickhouse_is_down(
    client: TestClient, world: World, app_state: AppState
) -> None:
    """NFR-R-06: ClickHouse errors degrade evidence to an empty list; profiles come from
    Postgres and the request still succeeds."""
    from payintel.api.read import ClickHouseEvidence

    class _Down:
        def query(self, *args: object, **kwargs: object) -> object:
            raise ConnectionError("clickhouse unreachable")

    app_state.evidence = ClickHouseEvidence(_Down())
    r = client.get("/v1/stores/alpha-shop.de", headers=auth(world.api_key))
    assert r.status_code == 200
    body = r.json()
    assert body["providers"][0]["id"] == "adyen" and body["providers"][0]["evidence"] == []
    r = client.get("/v1/stores", headers=auth(world.api_key), params={"provider": "adyen"})
    assert r.status_code == 200 and r.json()["items"]


PUBLIC_ROUTES = {
    "/portal/login",
    "/portal/login/totp",
    "/portal/login/enrol",
    "/portal/logout",
    "/v1/openapi.json",
    "/v1/docs",
}


def app_routes(client: TestClient) -> list[tuple[str, str, Any]]:
    """Every (method, path, route) of the application, mounted routers flattened."""
    stack: list[Any] = list(client.app.routes)  # type: ignore[attr-defined]
    out: list[tuple[str, str, Any]] = []
    while stack:
        route = stack.pop()
        inner = getattr(route, "original_router", None)
        if inner is not None:
            stack.extend(inner.routes)
            continue
        path = getattr(route, "path", "")
        for method in sorted((getattr(route, "methods", None) or set()) - {"HEAD", "OPTIONS"}):
            out.append((method, path, route))
    return sorted(out, key=lambda t: (t[1], t[0]))


def probe_path(path: str, route: Any = None) -> str:
    """The path with every parameter filled by a well-typed dummy (so the probe reaches
    the authorisation check, not request validation)."""
    kinds = {
        f.name: repr(f.field_info.annotation)
        for f in getattr(getattr(route, "dependant", None), "path_params", [])
    }

    def fill(m: re.Match[str]) -> str:
        kind = kinds.get(m.group(1), "")
        if "UUID" in kind:
            return "00000000-0000-4000-8000-000000000001"
        return "1" if "int" in kind else "x"

    return re.sub(r"\{([^}:]+)[^}]*\}", fill, path)


def form_for(route: Any, csrf: str) -> dict[str, Any]:
    """CSRF token plus a dummy value for every required form field, so the probe reaches
    the role check instead of stopping at request validation."""
    data: dict[str, Any] = {"csrf": csrf}
    for field in getattr(getattr(route, "dependant", None), "body_params", []):
        if field.name != "csrf" and field.field_info.is_required():
            data[field.name] = ["x"] if "list" in repr(field.field_info.annotation) else "x"
    return data


def staff_roles_of(route: Any) -> set[str] | None:
    """Staff roles admitted by a `ui.staff_roles(...)` dependency; None = any staff."""
    dependant = getattr(route, "dependant", None)
    stack = list(dependant.dependencies) if dependant is not None else []
    while stack:
        dep = stack.pop()
        stack.extend(dep.dependencies)
        call = dep.call
        if getattr(call, "__qualname__", "") == "staff_roles.<locals>.dep":
            for cell in call.__closure__ or ():
                roles = cell.cell_contents
                if isinstance(roles, tuple):
                    return {r.value for r in roles}
    return None


def test_no_route_is_reachable_without_authentication(client: TestClient, world: World) -> None:
    """NFR-S-04: every /v1, /portal and /admin route answers an anonymous request with a
    denial (401/403) or a redirect to the login page, never with content.

    Routes that are public by design are listed explicitly; a new unprotected endpoint
    fails this test."""
    checked = 0
    offenders: list[str] = []
    for method, path, _route in app_routes(client):
        if not path.startswith(("/v1/", "/portal", "/admin")) or path in PUBLIC_ROUTES:
            continue
        r = client.request(method, probe_path(path), follow_redirects=False)
        checked += 1
        ok = r.status_code in (401, 403) or (
            r.status_code == 303 and r.headers.get("location", "").startswith("/portal/login")
        )
        if not ok:
            offenders.append(f"{method} {path} -> {r.status_code}")
    assert checked > 50
    assert not offenders, offenders


def test_role_matrix_denies_every_other_role_on_every_route(
    client: TestClient,
    db_session: Session,
    world: World,
    app_state: AppState,
    fixed_clock: FixedClock,
) -> None:
    """NFR-S-04 role × endpoint matrix, generated from the application's route table:

    - an organisation admin gets 403 on every /admin route;
    - a staff role outside a route's `staff_roles(...)` list gets 403 on it (support
      on every compliance/analyst/admin-only admin route);
    - an organisation viewer gets 403 on every mutating /portal route, with a valid CSRF
      token and every required form field present (ТЗ 3.2: viewer is read-only);
    - an API key carrying only `usage:read` gets 403 `scope_missing` on every /v1 route
      that is not the usage or reference endpoint.

    A new route is covered automatically; the expected answer for every probe is a
    role denial, never content, a redirect or a validation error."""
    routes = app_routes(client)
    offenders: list[str] = []
    checked = 0

    def deny(method: str, path: str, r: Any, who: str, expect: tuple[int, ...] = (403,)) -> None:
        nonlocal checked
        checked += 1
        if r.status_code not in expect:
            offenders.append(f"{who}: {method} {path} -> {r.status_code}")

    # 1. organisation admin on the admin UI
    csrf = login(client, world.org_admin_email, state=app_state, session=db_session)
    for method, path, route in routes:
        if path.startswith("/admin"):
            r = client.request(
                method, probe_path(path, route), data=form_for(route, csrf), follow_redirects=False
            )
            deny(method, path, r, "org_admin")
    client.cookies.clear()

    # 2. support staff on compliance/analyst/admin-only admin routes
    csrf = login(client, world.staff_support_email, state=app_state, session=db_session)
    support_checked = 0
    for method, path, route in routes:
        roles = staff_roles_of(route)
        if path.startswith("/admin") and roles is not None and "staff_support" not in roles:
            r = client.request(
                method, probe_path(path, route), data=form_for(route, csrf), follow_redirects=False
            )
            deny(method, path, r, "staff_support")
            support_checked += 1
    assert support_checked > 20
    client.cookies.clear()

    # 3. organisation viewer on every mutating portal route
    csrf = login(client, world.org_viewer_email, state=app_state, session=db_session)
    viewer_checked = 0
    for method, path, route in routes:
        if path.startswith("/portal") and method != "GET" and path not in PUBLIC_ROUTES:
            r = client.request(
                method, probe_path(path, route), data=form_for(route, csrf), follow_redirects=False
            )
            deny(method, path, r, "org_viewer")
            viewer_checked += 1
    assert viewer_checked > 10
    client.cookies.clear()

    # 4. API key with a single scope on every /v1 route
    key = issue_key(
        db_session, world.org, clock=fixed_clock, settings=app_state.settings, scopes=["usage:read"]
    )
    for method, path, _route in routes:
        if not path.startswith("/v1/") or path in PUBLIC_ROUTES:
            continue
        if path == "/v1/usage" or path in ("/v1/providers", "/v1/payment-methods"):
            continue  # usage:read covers /v1/usage; reference lists need no scope (FR-API-02)
        fixed_clock.advance(seconds=1)  # one request per rate-limit window (FR-API-07)
        r = client.request(method, probe_path(path, _route), headers=auth(key.raw), json={})
        deny(method, path, r, "key[usage:read]")
        if r.status_code == 403 and r.json().get("reason") != "scope_missing":
            offenders.append(f"key[usage:read]: {method} {path} -> {r.json().get('reason')}")
    assert checked > 80, checked
    assert not offenders, "\n".join(offenders)
