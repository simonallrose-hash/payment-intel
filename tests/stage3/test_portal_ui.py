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
    "/admin/abuse",
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


def test_store_views_are_journaled(
    client: TestClient, db_session: Session, world: World, app_state: AppState
) -> None:
    """FR-UI-04: reading a store card (not only mutations) leaves an audit row with the
    actor, the store, the field profile and the client IP."""
    login(client, world.org_viewer_email, state=app_state, session=db_session)
    assert client.get("/portal/stores/beta-store.de").status_code == 200
    assert client.get("/portal/stores/beta-store.de/screenshot").status_code == 404
    rows = (
        db_session.execute(
            select(AuditLog).where(AuditLog.action == "portal.view_store").order_by(AuditLog.id)
        )
        .scalars()
        .all()
    )
    assert [(r.object_type, r.object_id) for r in rows] == [("store", "beta-store.de")]
    assert rows[0].actor == f"org_viewer:{world.org_viewer_email}"
    assert rows[0].after == {"profile": "c1_full"}
    assert client.get("/portal/stores/delta-boutique.fr").status_code == 403
    denied = (
        db_session.execute(
            select(AuditLog).where(
                AuditLog.action == "portal.view_store", AuditLog.object_id == "delta-boutique.fr"
            )
        )
        .scalars()
        .all()
    )
    assert denied == []  # a refused lookup writes nothing: the card was never shown


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


def test_bot_page_names_agent_ip_ranges_rules_and_optout(
    client: TestClient, app_state: AppState
) -> None:
    """FR-OO-01: the public bot page carries purpose, UA, IP ranges, politeness and opt-out."""
    from payintel.compliance import bot_page

    page = bot_page.build(app_state.settings)
    r = client.get("/bot")
    assert r.status_code == 200
    assert page.user_agent in r.text and page.contact_email in r.text
    for needle in ("robots.txt", 'href="/optout"', "dns_txt", "well_known", "never"):
        assert needle in r.text, needle
    assert str(page.max_rps_per_host).rstrip("0").rstrip(".") in r.text
    if page.ip_ranges:
        assert all(cidr in r.text for cidr in page.ip_ranges)
    else:
        assert "published on request" in r.text


def test_ui_is_english_desktop_only(
    client: TestClient, db_session: Session, world: World, app_state: AppState
) -> None:
    """FR-UI-05: English UI, fixed minimum width 1280px, no mobile layout."""
    login(client, world.org_admin_email, state=app_state, session=db_session)
    r = client.get("/portal/stores")
    assert r.status_code == 200 and '<html lang="en">' in r.text
    css = client.get("/static/app.css")
    assert css.status_code == 200 and "min-width:1280px" in css.text.replace(" ", "")
    assert "@media" not in css.text


def test_portal_watchlist_csv_upload(
    client: TestClient, db_session: Session, world: World, app_state: AppState
) -> None:
    """FR-AL-01: domains come in through a CSV upload on the portal, within the limit."""
    csrf = login(client, world.org_admin_email, state=app_state, session=db_session)
    r = client.post(
        "/portal/watchlists", data={"name": "csv", "csrf": csrf}, follow_redirects=False
    )
    assert r.status_code == 303
    wl_url = r.headers["location"].split("?")[0]
    csv_body = "domain\nAlpha-Shop.de\nhttps://beta-store.de/path\nnot a domain\nalpha-shop.de\n"
    r = client.post(
        f"{wl_url}/domains",
        data={"csrf": csrf},
        files={"csv_file": ("list.csv", csv_body.encode(), "text/csv")},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    from urllib.parse import unquote_plus

    assert "Added 2" in unquote_plus(r.headers["location"])
    page = client.get(wl_url)
    assert "alpha-shop.de" in page.text and "beta-store.de" in page.text


def test_store_card_shows_evidence_for_full_profile_only(
    client: TestClient, db_session: Session, world: World, app_state: AppState
) -> None:
    """FR-UI-03: confidence, first/last seen for everyone; detection method only for c1_full."""
    from collections.abc import Sequence

    from payintel.api.read import EvidenceKey
    from payintel.api.schemas.common import Evidence

    class StubEvidence:
        def evidence(self, host_id: int, provider_id: str, limit: int) -> list[Evidence]:
            return [
                Evidence(signal_type="network_host", value="api.stripe.com", page_type="checkout")
            ]

        def evidence_many(
            self, keys: Sequence[EvidenceKey], limit: int
        ) -> dict[EvidenceKey, list[Evidence]]:
            return {k: self.evidence(k[0], k[1], limit) for k in keys}

    app_state.evidence = StubEvidence()
    login(client, world.org_admin_email, state=app_state, session=db_session)
    r = client.get("/portal/stores/beta-store.de")
    assert r.status_code == 200
    for needle in (
        "network_host",
        "api.stripe.com",
        "Detection evidence",
        "2026-09-08",
        "2026-10-06",
    ):
        assert needle in r.text, needle
    client.cookies.clear()
    from payintel.core.clock import FixedClock
    from payintel.core.models.base import Role
    from tests.stage3.conftest import new_user

    assert isinstance(app_state.clock, FixedClock)
    new_user(
        db_session, "fr-admin@payintel.test", Role.ORG_ADMIN, world.other_org, clock=app_state.clock
    )
    db_session.flush()
    login(client, "fr-admin@payintel.test", state=app_state, session=db_session)
    r = client.get("/portal/stores/delta-boutique.fr")
    assert r.status_code == 200
    assert "Detection evidence" not in r.text and "network_host" not in r.text
    assert "2026-09-08" in r.text and "high" in r.text


def test_crawler_page_lists_worker_heartbeats_with_host_load(
    client: TestClient,
    db_session: Session,
    world: World,
    app_state: AppState,
    fixed_clock: FixedClock,
) -> None:
    """FR-ADM-03 «загрузка серверов»: the admin crawler page shows every worker's host,
    load, RAM and in-flight count from its heartbeat, and flags silent workers."""
    from payintel.core import heartbeat as hb

    load = hb.HostLoad("crawl-1", 4242, 3.5, 2.0, 4, 16000, 12000)
    for i in range(2):  # the same worker twice: one row, started_at kept, counters updated
        hb.beat(
            db_session,
            worker_id="worker-light-0",
            kind="light",
            concurrency=200,
            in_flight=150 + i,
            done=10 * i,
            load=load,
            clock=fixed_clock,
        )
        fixed_clock.advance(seconds=30)
    hb.beat(
        db_session,
        worker_id="worker-checkout-0",
        kind="checkout",
        concurrency=4,
        in_flight=0,
        done=7,
        load=hb.HostLoad("crawl-2", 7, 0.1, 0.1, 8, 32000, 8000),
        stopped=True,
        clock=fixed_clock,
    )
    db_session.flush()
    fixed_clock.advance(seconds=200)  # > 3 intervals since the light worker's last beat
    views = {v.row.worker_id: v for v in hb.workers(db_session, now=fixed_clock.now())}
    light = views["worker-light-0"]
    assert light.state == "stale" and light.row.in_flight == 151 and light.row.scans_done == 10
    assert light.row.started_at < light.row.updated_at
    assert views["worker-checkout-0"].state == "stopped"

    login(client, world.staff_support_email, state=app_state, session=db_session)
    r = client.get("/admin/crawler")
    assert r.status_code == 200
    for needle in ("crawl-1", "151 / 200", "3.50 / 2.00", "12000 / 16000 MB", "75.0 %", "stale"):
        assert needle in r.text, needle
    assert "crawl-2" in r.text and "stopped" in r.text
