"""Portal and admin pages: login with mandatory TOTP, CSRF, lockout, idle expiry, CSP,
role separation (FR-UI-01…05, FR-ADM-04, NFR-S-05)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.api.deps import AppState
from payintel.core.clock import FixedClock
from payintel.core.models.audit import AuditLog
from tests.stage3.conftest import PASSWORD, World, login

pytestmark = pytest.mark.integration

PORTAL_PAGES = [
    "/portal",
    "/portal/stores",
    "/portal/stores?country=DE",
    "/portal/stores/alpha-shop.de",
    "/portal/watchlists",
    "/portal/alerts",
    "/portal/exports",
    "/portal/reports",
    "/portal/keys",
    "/portal/users",
    "/portal/usage",
    "/portal/docs",
]
ADMIN_PAGES = [
    "/admin",
    "/admin/orgs",
    "/admin/users",
    "/admin/rules",
    "/admin/rules/new",
    "/admin/crawler",
    "/admin/domains?q=alpha-shop.de",
    "/admin/flags",
    "/admin/quality",
    "/admin/review",
    "/admin/optout",
    "/admin/dsar",
    "/admin/exports",
    "/admin/reports",
    "/admin/audit?verify=1",
]


def test_anonymous_is_redirected_to_login(client: TestClient, world: World) -> None:
    r = client.get("/portal/stores", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/portal/login?next=")
    assert client.get("/admin", follow_redirects=False).status_code == 303
    assert client.get("/bot").status_code == 200  # public


def test_password_alone_is_not_enough(client: TestClient, world: World) -> None:
    r = client.post(
        "/portal/login",
        data={"email": world.org_admin_email, "password": PASSWORD},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"].startswith("/portal/login/enrol")
    assert "pi_session" in r.cookies
    r = client.get("/portal/stores", follow_redirects=False)
    assert r.status_code == 303 and "/portal/login/totp" in r.headers["location"]


def test_wrong_password_and_lockout(
    client: TestClient, db_session: Session, world: World, app_state: AppState
) -> None:
    for _ in range(app_state.settings.api.login_max_attempts):
        r = client.post(
            "/portal/login", data={"email": world.org_admin_email, "password": "wrong-password-xx"}
        )
        assert r.status_code == 401
    r = client.post("/portal/login", data={"email": world.org_admin_email, "password": PASSWORD})
    assert r.status_code == 423
    failed = db_session.execute(
        select(AuditLog).where(AuditLog.action == "auth.login_failed")
    ).scalars()
    assert len(list(failed)) == app_state.settings.api.login_max_attempts
    r = client.post("/portal/login", data={"email": "nobody@x.test", "password": "whatever-12345"})
    assert r.status_code == 401  # same answer for unknown users


def test_full_login_enrols_totp_and_opens_portal(
    client: TestClient, db_session: Session, world: World, app_state: AppState
) -> None:
    login(client, world.org_admin_email, state=app_state, session=db_session)
    r = client.get("/portal")
    assert r.status_code == 200 and "Client GmbH" in r.text
    actions = [
        a
        for a in db_session.execute(select(AuditLog.action).order_by(AuditLog.id)).scalars()
        if a.startswith("auth.")
    ]
    assert actions[-2:] == ["auth.password_ok", "auth.totp_enrolled"]
    # second login uses the stored secret
    client.cookies.clear()
    login(client, world.org_admin_email, state=app_state, session=db_session)
    assert client.get("/portal/stores").status_code == 200
    assert "auth.login" in list(db_session.execute(select(AuditLog.action)).scalars())


def test_wrong_totp_code(
    client: TestClient, db_session: Session, world: World, app_state: AppState
) -> None:
    login(client, world.org_admin_email, state=app_state, session=db_session)
    client.cookies.clear()
    client.post("/portal/login", data={"email": world.org_admin_email, "password": PASSWORD})
    page = client.get("/portal/login/totp").text
    csrf = page.split('name="csrf" value="')[1].split('"')[0]
    r = client.post("/portal/login/totp", data={"csrf": csrf, "code": "000000"})
    assert r.status_code == 401
    assert client.get("/portal/stores", follow_redirects=False).status_code == 303


@pytest.mark.parametrize("path", PORTAL_PAGES)
def test_portal_pages_render_for_org_user(
    client: TestClient, db_session: Session, world: World, app_state: AppState, path: str
) -> None:
    login(client, world.org_admin_email, state=app_state, session=db_session)
    r = client.get(path)
    assert r.status_code == 200, (path, r.text[:300])
    assert "9.1.2" not in r.text and "google-analytics.com" not in r.text  # AS-23
    csp = r.headers["content-security-policy"]
    assert "unsafe-inline" not in csp and "script-src 'self'" in csp
    assert r.headers["x-frame-options"] == "DENY"


def test_org_user_cannot_open_admin(
    client: TestClient, db_session: Session, world: World, app_state: AppState
) -> None:
    login(client, world.org_admin_email, state=app_state, session=db_session)
    r = client.get("/admin/orgs")
    assert r.status_code == 403 and "text/html" in r.headers["content-type"]


@pytest.mark.parametrize("path", ADMIN_PAGES)
def test_admin_pages_render_for_staff_admin(
    client: TestClient, db_session: Session, world: World, app_state: AppState, path: str
) -> None:
    login(client, world.staff_admin_email, state=app_state, session=db_session)
    r = client.get(path)
    assert r.status_code == 200, (path, r.text[:300])


def test_internal_lookup_is_analyst_only(
    client: TestClient, db_session: Session, world: World, app_state: AppState
) -> None:
    login(client, world.staff_support_email, state=app_state, session=db_session)
    assert client.get("/admin/domains?q=alpha-shop.de").status_code == 403
    assert client.get("/admin/orgs").status_code == 200
    client.cookies.clear()
    login(client, world.staff_analyst_email, state=app_state, session=db_session)
    r = client.get("/admin/domains?q=alpha-shop.de")
    assert r.status_code == 200 and "google-analytics.com" in r.text and "9.1.2" in r.text
    r = client.get("/admin/domains?q=czds-only.de")
    assert "CZDS-only" in r.text


def test_csrf_is_required_on_forms(
    client: TestClient, db_session: Session, world: World, app_state: AppState
) -> None:
    csrf = login(client, world.org_admin_email, state=app_state, session=db_session)
    r = client.post("/portal/watchlists", data={"name": "no-token"})
    assert r.status_code == 403
    r = client.post("/portal/watchlists", data={"name": "bad", "csrf": "nope"})
    assert r.status_code == 403
    r = client.post("/portal/watchlists", data={"name": "ok", "csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/portal/watchlists/")


def test_cross_origin_login_post_is_rejected(client: TestClient, world: World) -> None:
    r = client.post(
        "/portal/login",
        data={"email": world.org_admin_email, "password": PASSWORD},
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 403


def test_viewer_cannot_change_but_admin_can(
    client: TestClient, db_session: Session, world: World, app_state: AppState
) -> None:
    csrf = login(client, world.org_viewer_email, state=app_state, session=db_session)
    assert client.get("/portal/keys").status_code == 200
    r = client.post("/portal/keys", data={"csrf": csrf, "name": "k", "scopes": ["stores:read"]})
    assert r.status_code == 403
    client.cookies.clear()
    csrf = login(client, world.org_admin_email, state=app_state, session=db_session)
    r = client.post("/portal/keys", data={"csrf": csrf, "name": "k", "scopes": ["stores:read"]})
    assert r.status_code == 200 and "pik_" in r.text and "shown once" in r.text


def test_session_expires_after_idle(
    client: TestClient,
    db_session: Session,
    world: World,
    app_state: AppState,
    fixed_clock: FixedClock,
) -> None:
    login(client, world.org_admin_email, state=app_state, session=db_session)
    assert client.get("/portal").status_code == 200
    fixed_clock.advance(seconds=11 * 3600)
    assert client.get("/portal").status_code == 200  # activity refreshes last_seen
    fixed_clock.advance(seconds=12 * 3600 + 1)
    assert client.get("/portal", follow_redirects=False).status_code == 303


def test_logout_revokes_session(
    client: TestClient, db_session: Session, world: World, app_state: AppState
) -> None:
    csrf = login(client, world.org_admin_email, state=app_state, session=db_session)
    r = client.post("/portal/logout", data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/portal", follow_redirects=False).status_code == 303


def test_staff_lands_on_admin(
    client: TestClient, db_session: Session, world: World, app_state: AppState
) -> None:
    login(client, world.staff_admin_email, state=app_state, session=db_session)
    r = client.get("/portal", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin"


def test_store_card_shows_detection_details(
    client: TestClient, db_session: Session, world: World, app_state: AppState
) -> None:
    login(client, world.org_admin_email, state=app_state, session=db_session)
    r = client.get("/portal/stores/beta-store.de")
    assert r.status_code == 200
    for needle in ("stripe", "klarna", "bnpl", "high", "2026-09-08", "provider_added"):
        assert needle in r.text, needle
    assert client.get("/portal/stores/delta-boutique.fr").status_code == 403
    assert client.get("/portal/stores/czds-only.de").status_code == 404


def test_security_headers_cookie_flags_and_support_is_read_only(
    client: TestClient, db_session: Session, world: World, app_state: AppState
) -> None:
    r = client.get("/portal/login")
    csp = r.headers["content-security-policy"]
    assert "unsafe-inline" not in csp and "script-src 'self'" in csp
    assert r.headers["x-frame-options"] == "DENY" and r.headers["cache-control"] == "no-store"
    r = client.post(
        "/portal/login",
        data={"email": world.staff_support_email, "password": PASSWORD},
        follow_redirects=False,
    )
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    csrf = login(client, world.staff_support_email, state=app_state, session=db_session)
    assert client.get("/admin/orgs").status_code == 200
    r = client.post("/admin/flags/feature_c2_enabled", data={"csrf": csrf, "enabled": "true"})
    assert r.status_code == 403
    r = client.post(
        "/admin/orgs", data={"csrf": csrf, "legal_name": "X", "country": "DE", "reg_number": "1"}
    )
    assert r.status_code == 403
