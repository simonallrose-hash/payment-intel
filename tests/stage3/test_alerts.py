"""AC-09 and FR-AL-01…04: two confirming scans → provider_added → signed webhook, retries."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.alerts import delivery as dl
from payintel.alerts import matcher
from payintel.alerts import webhook as wh
from payintel.alerts.telegram import TelegramClient
from payintel.alerts.worker import run_cycle
from payintel.api.deps import AppState
from payintel.core.clock import FixedClock
from payintel.core.models.alerts import Delivery, Webhook
from payintel.core.models.base import (
    ChangeEventType,
    ConfidenceLevel,
    DeliveryStatus,
    ProviderRole,
    ScanStatus,
    ScanType,
)
from payintel.core.models.scans import ScanRun
from payintel.core.models.store import ChangeEvent
from payintel.history.differ import CheckoutObservation, SeenProvider, apply_checkout_observation
from tests.stage3.conftest import World, auth

pytestmark = pytest.mark.integration


class Receiver:
    """Fake webhook endpoint: records requests, answers with a configurable status."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.BaseTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(self.status)

        return httpx.MockTransport(handler)


def _scan(db_session: Session, host_id: int, providers: list[str], at: object) -> None:
    run = ScanRun(
        id=uuid.uuid4(),
        host_id=host_id,
        scan_type=ScanType.CHECKOUT,
        started_at=at,
        finished_at=at,
        status=ScanStatus.REACHED_PAYMENT_STEP,
        worker_id="t",
        ruleset_version="t",
    )
    db_session.add(run)
    db_session.flush()
    apply_checkout_observation(
        db_session,
        host_id,
        CheckoutObservation(
            scan_run_id=run.id,
            status=ScanStatus.REACHED_PAYMENT_STEP,
            providers=[
                SeenProvider(p, ProviderRole.GATEWAY, ConfidenceLevel.HIGH, 0.9) for p in providers
            ],
        ),
        now=at,  # type: ignore[arg-type]
    )


def _setup_rule(
    client: TestClient, world: World, url: str = "https://hooks.client.example/payintel"
) -> tuple[int, str, int]:
    headers = auth(world.api_key)
    wl = client.post(
        "/v1/watchlists", json={"name": "psp", "domains": ["alpha-shop.de"]}, headers=headers
    )
    assert wl.status_code == 201, wl.text
    hook = client.post("/v1/webhooks", json={"url": url}, headers=headers)
    assert hook.status_code == 201, hook.text
    secret = hook.json()["secret"]
    assert secret.startswith("whsec_")
    assert "secret" not in client.get(f"/v1/webhooks/{hook.json()['id']}", headers=headers).json()
    return wl.json()["id"], secret, hook.json()["id"]


def _rule(
    db_session: Session,
    world: World,
    app_state: AppState,
    *,
    watchlist_id: int,
    webhook_id: int,
    digest: str = "immediate",
) -> int:
    from payintel.alerts import rules

    rule = rules.create_rule(
        db_session,
        org_id=world.org.id,
        watchlist_id=watchlist_id,
        event_types=[ChangeEventType.PROVIDER_ADDED.value, ChangeEventType.PROVIDER_REMOVED.value],
        provider_id=None,
        method_id=None,
        min_confidence="medium",
        channel="webhook",
        webhook_id=webhook_id,
        telegram_chat_id=None,
        digest=digest,
        actor="test",
        clock=app_state.clock,
    )
    return rule.id


def test_psp_change_needs_two_scans_then_signed_webhook(
    client: TestClient,
    db_session: Session,
    world: World,
    app_state: AppState,
    fixed_clock: FixedClock,
) -> None:
    wl_id, secret, hook_id = _setup_rule(client, world)
    _rule(db_session, world, app_state, watchlist_id=wl_id, webhook_id=hook_id)
    host = world.hosts["alpha-shop.de"]
    receiver = Receiver()
    sender = dl.WebhookSender(
        app_state.secret_box, app_state.settings, transport=receiver.transport(), clock=fixed_clock
    )
    # scan 1: stripe appears next to adyen → no event yet (needs 2 confirmations)
    _scan(db_session, host.id, ["adyen", "stripe"], fixed_clock.now())
    events = list(
        db_session.execute(
            select(ChangeEvent).where(
                ChangeEvent.entity_id == "stripe", ChangeEvent.host_id == host.id
            )
        ).scalars()
    )
    assert events == []
    r = run_cycle(
        app_state.session_factory,
        settings=app_state.settings,
        clock=fixed_clock,
        webhook_sender=sender,
        telegram_sender=None,
    )
    assert r.matched.deliveries_created == 0 and receiver.requests == []
    # scan 2: confirmed → provider_added → one delivery → webhook with valid signature
    fixed_clock.advance(days=1)
    _scan(db_session, host.id, ["adyen", "stripe"], fixed_clock.now())
    events = list(
        db_session.execute(
            select(ChangeEvent).where(
                ChangeEvent.entity_id == "stripe", ChangeEvent.host_id == host.id
            )
        ).scalars()
    )
    assert [e.event_type for e in events] == [ChangeEventType.PROVIDER_ADDED]
    r = run_cycle(
        app_state.session_factory,
        settings=app_state.settings,
        clock=fixed_clock,
        webhook_sender=sender,
        telegram_sender=None,
    )
    assert r.matched.deliveries_created == 1 and r.dispatched.delivered == 1
    assert len(receiver.requests) == 1
    req = receiver.requests[0]
    header = req.headers[app_state.settings.alerts.signature_header]
    assert header.startswith("t=") and ",v1=" in header
    assert wh.verify(secret, req.content, header, now_ts=wh.unix_ts(fixed_clock.now()))
    assert not wh.verify("whsec_wrong", req.content, header, now_ts=wh.unix_ts(fixed_clock.now()))
    body = json.loads(req.content)
    assert (
        body["domain"] == "alpha-shop.de"
        and body["type"] == "provider_added"
        and body["entity"] == "stripe"
    )
    # removal after two misses, and no duplicate delivery for the same event
    fixed_clock.advance(days=1)
    _scan(db_session, host.id, ["adyen"], fixed_clock.now())
    fixed_clock.advance(days=1)
    _scan(db_session, host.id, ["adyen"], fixed_clock.now())
    r = run_cycle(
        app_state.session_factory,
        settings=app_state.settings,
        clock=fixed_clock,
        webhook_sender=sender,
        telegram_sender=None,
    )
    assert r.matched.deliveries_created == 1
    assert json.loads(receiver.requests[-1].content)["type"] == "provider_removed"
    r = run_cycle(
        app_state.session_factory,
        settings=app_state.settings,
        clock=fixed_clock,
        webhook_sender=sender,
        telegram_sender=None,
    )
    assert r.matched.deliveries_created == 0 and len(receiver.requests) == 2
    # the delivery journal is visible to the organisation
    rows = dl.deliveries_of_org(db_session, world.org.id)
    assert [d.status for d, _ in rows] == [DeliveryStatus.DELIVERED, DeliveryStatus.DELIVERED]


def test_webhook_retries_five_times_within_24h_then_fails(
    client: TestClient,
    db_session: Session,
    world: World,
    app_state: AppState,
    fixed_clock: FixedClock,
) -> None:
    wl_id, _secret, hook_id = _setup_rule(client, world)
    _rule(db_session, world, app_state, watchlist_id=wl_id, webhook_id=hook_id)
    host = world.hosts["alpha-shop.de"]
    receiver = Receiver(status=500)
    sender = dl.WebhookSender(
        app_state.secret_box, app_state.settings, transport=receiver.transport(), clock=fixed_clock
    )
    _scan(db_session, host.id, ["adyen", "stripe"], fixed_clock.now())
    fixed_clock.advance(days=1)
    _scan(db_session, host.id, ["adyen", "stripe"], fixed_clock.now())
    start = fixed_clock.now()
    attempts = 0
    for _ in range(40):
        r = run_cycle(
            app_state.session_factory,
            settings=app_state.settings,
            clock=fixed_clock,
            webhook_sender=sender,
            telegram_sender=None,
        )
        attempts += r.dispatched.attempted
        fixed_clock.advance(seconds=3600)
    d = db_session.execute(select(Delivery)).scalar_one()
    assert d.status == DeliveryStatus.FAILED and d.attempts == 5 and d.last_error
    assert attempts == 5 and len(receiver.requests) == 5
    assert all((req.extensions, True) for req in receiver.requests)
    assert fixed_clock.now() - start <= timedelta(hours=40)
    assert d.delivered_at is None


def test_daily_digest_groups_events(
    client: TestClient,
    db_session: Session,
    world: World,
    app_state: AppState,
    fixed_clock: FixedClock,
) -> None:
    wl_id, _secret, hook_id = _setup_rule(client, world)
    _rule(db_session, world, app_state, watchlist_id=wl_id, webhook_id=hook_id, digest="daily")
    host = world.hosts["alpha-shop.de"]
    receiver = Receiver()
    sender = dl.WebhookSender(
        app_state.secret_box, app_state.settings, transport=receiver.transport(), clock=fixed_clock
    )
    _scan(db_session, host.id, ["adyen", "stripe", "paypal"], fixed_clock.now())
    fixed_clock.advance(days=1)
    _scan(db_session, host.id, ["adyen", "stripe", "paypal"], fixed_clock.now())
    r = run_cycle(
        app_state.session_factory,
        settings=app_state.settings,
        clock=fixed_clock,
        webhook_sender=sender,
        telegram_sender=None,
    )
    assert r.matched.deliveries_created == 2 and r.dispatched.attempted == 0  # not digest time
    r = run_cycle(
        app_state.session_factory,
        settings=app_state.settings,
        clock=fixed_clock,
        webhook_sender=sender,
        telegram_sender=None,
        force_digests=True,
    )
    assert r.dispatched.delivered == 2 and len(receiver.requests) == 1
    body = json.loads(receiver.requests[0].content)
    assert body["digest"] == "daily" and {e["entity"] for e in body["events"]} == {
        "stripe",
        "paypal",
    }


def test_telegram_delivery_uses_bot_api(
    client: TestClient,
    db_session: Session,
    world: World,
    app_state: AppState,
    fixed_clock: FixedClock,
) -> None:
    from payintel.alerts import rules

    wl_id, _s, _h = _setup_rule(client, world)
    rules.create_rule(
        db_session,
        org_id=world.org.id,
        watchlist_id=wl_id,
        event_types=["provider_added"],
        provider_id="stripe",
        method_id=None,
        min_confidence="low",
        channel="telegram",
        webhook_id=None,
        telegram_chat_id="-100123",
        digest="immediate",
        actor="test",
        clock=fixed_clock,
    )
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"ok": True})

    tg = dl.TelegramSender(
        TelegramClient(
            "123:token", api_base="https://api.telegram.org", transport=httpx.MockTransport(handler)
        )
    )
    host = world.hosts["alpha-shop.de"]
    _scan(db_session, host.id, ["adyen", "stripe"], fixed_clock.now())
    fixed_clock.advance(days=1)
    _scan(db_session, host.id, ["adyen", "stripe"], fixed_clock.now())
    r = run_cycle(
        app_state.session_factory,
        settings=app_state.settings,
        clock=fixed_clock,
        webhook_sender=None,
        telegram_sender=tg,
    )
    assert r.dispatched.delivered == 1 and len(calls) == 1
    assert calls[0].url.path.endswith("/sendMessage")
    payload = json.loads(calls[0].content)
    assert payload["chat_id"] == "-100123" and "alpha-shop.de" in payload["text"]


def test_watchlist_limit_and_csv_import(
    client: TestClient, db_session: Session, world: World
) -> None:
    headers = auth(world.api_key)
    r = client.post("/v1/watchlists", json={"name": "big"}, headers=headers)
    wid = r.json()["id"]
    domains = [f"shop{i}.de" for i in range(49)]
    r = client.post(f"/v1/watchlists/{wid}/domains", json={"domains": domains}, headers=headers)
    assert r.status_code == 200 and r.json()["added"] == 49
    r = client.post(
        f"/v1/watchlists/{wid}/domains", json={"domains": ["a.de", "b.de"]}, headers=headers
    )
    assert r.status_code == 403 and r.json()["reason"] == "watchlist_limit_exceeded"
    r = client.post(f"/v1/watchlists/{wid}/domains", json={"domains": ["ok.de"]}, headers=headers)
    assert r.status_code == 200 and r.json()["total"] == 50
    r = client.delete(f"/v1/watchlists/{wid}/domains/shop1.de", headers=headers)
    assert r.status_code == 204
    r = client.get(f"/v1/watchlists/{wid}/domains", headers=headers)
    assert r.status_code == 200 and "shop1.de" not in r.text


def test_webhook_secret_is_encrypted_at_rest(
    client: TestClient, db_session: Session, world: World, app_state: AppState
) -> None:
    _wl, secret, hook_id = _setup_rule(client, world)
    hook = db_session.get(Webhook, hook_id)
    assert (
        hook is not None
        and secret not in hook.secret_encrypted
        and hook.secret_encrypted.startswith("v1:")
    )
    assert wh.decrypt_secret(hook, app_state.secret_box) == secret


def test_matcher_cursor_skips_invisible_events(
    db_session: Session, world: World, app_state: AppState
) -> None:
    r = matcher.match_new_events(db_session, settings=app_state.settings, clock=app_state.clock)
    assert r.events_seen >= 0 and r.last_event_id > 0
    again = matcher.match_new_events(db_session, settings=app_state.settings, clock=app_state.clock)
    assert again.events_seen == 0 and again.last_event_id == r.last_event_id
