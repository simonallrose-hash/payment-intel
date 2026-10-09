"""AC-07 and FR-API-02/05/06/07/08/09/10 over the real application.

The 403 matrix: endpoint × (missing key, revoked, expired, wrong IP, missing
scope, outside segment, suspended organisation, contract out of term, no
entitlement, other organisation's objects) → `application/problem+json` with
a stable `reason`. Every C1 payload is scanned for AS-23 keys.
"""

from __future__ import annotations

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
from tests.stage3.conftest import World, auth, issue_key, staff_principal

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
        return {s["domain"] for s in client.get(f"/v1/stores?{q}", headers=headers).json()["items"]}

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
