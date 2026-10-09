"""FR-AL-05 segment subscriptions without a watchlist and FR-QA-04 holds on
`provider_removed` spikes."""

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
from payintel.alerts import rules
from payintel.alerts.worker import run_cycle
from payintel.api.deps import AppState
from payintel.core.clock import FixedClock
from payintel.core.errors import ValidationError
from payintel.core.flags import FlagService
from payintel.core.models.alerts import Delivery
from payintel.core.models.base import (
    ChangeEventType,
    ConfidenceLevel,
    DeliveryStatus,
    ProviderRole,
    ScanStatus,
    ScanType,
)
from payintel.core.models.quality import QualityAlert
from payintel.core.models.scans import ScanRun
from payintel.core.models.store import ChangeEvent
from payintel.core.settings import Settings
from payintel.entitlements.check import resolve_grant
from payintel.history.differ import CheckoutObservation, SeenProvider, apply_checkout_observation
from payintel.quality import anomalies
from tests.stage3.conftest import World, auth, login

pytestmark = pytest.mark.integration


class Receiver:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.BaseTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(self.status)

        return httpx.MockTransport(handler)


def _scan(db_session: Session, host_id: int, providers: list[str], clock: FixedClock) -> None:
    at = clock.now()
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
        now=at,
    )


def _webhook(client: TestClient, world: World) -> int:
    hook = client.post(
        "/v1/webhooks", json={"url": "https://hooks.client.example/x"}, headers=auth(world.api_key)
    )
    assert hook.status_code == 201, hook.text
    return int(hook.json()["id"])


def _grant(db_session: Session, world: World, clock: FixedClock) -> object:
    flags = FlagService(db_session, Settings().flags, clock=clock)
    return resolve_grant(db_session, world.org.id, today=clock.now().date(), flags=flags)


def _segment_rule(
    db_session: Session,
    world: World,
    clock: FixedClock,
    *,
    webhook_id: int,
    countries: list[str] | None = None,
    platforms: list[str] | None = None,
    provider_id: str | None = None,
    event_types: list[str] | None = None,
) -> int:
    grant = _grant(db_session, world, clock)
    rule = rules.create_rule(
        db_session,
        org_id=world.org.id,
        watchlist_id=None,
        event_types=event_types or [ChangeEventType.PROVIDER_ADDED.value],
        provider_id=provider_id,
        method_id=None,
        min_confidence="medium",
        channel="webhook",
        webhook_id=webhook_id,
        telegram_chat_id=None,
        digest="immediate",
        actor="test",
        clock=clock,
        countries=countries,
        platforms=platforms,
        grant=grant,  # type: ignore[arg-type]
    )
    return rule.id


def _cycle(app_state: AppState, clock: FixedClock, sender: dl.WebhookSender) -> object:
    return run_cycle(
        app_state.session_factory,
        settings=app_state.settings,
        clock=clock,
        webhook_sender=sender,
        telegram_sender=None,
    )


def test_segment_rule_fires_without_watchlist_and_respects_filters(
    client: TestClient,
    db_session: Session,
    world: World,
    app_state: AppState,
    fixed_clock: FixedClock,
) -> None:
    hook_id = _webhook(client, world)
    # «new stores with provider stripe in DE» — no watchlist at all
    de_rule = _segment_rule(
        db_session, world, fixed_clock, webhook_id=hook_id, countries=["de"], provider_id="stripe"
    )
    # a platform filter that does not match alpha-shop (shopify)
    _segment_rule(db_session, world, fixed_clock, webhook_id=hook_id, platforms=["woocommerce"])
    receiver = Receiver()
    sender = dl.WebhookSender(
        app_state.secret_box, app_state.settings, transport=receiver.transport(), clock=fixed_clock
    )
    host = world.hosts["alpha-shop.de"]
    _scan(db_session, host.id, ["adyen", "stripe"], fixed_clock)
    fixed_clock.advance(days=1)
    _scan(db_session, host.id, ["adyen", "stripe"], fixed_clock)
    r = _cycle(app_state, fixed_clock, sender)
    assert r.matched.deliveries_created == 1 and r.dispatched.delivered == 1  # type: ignore[attr-defined]
    body = json.loads(receiver.requests[0].content)
    assert body["domain"] == "alpha-shop.de" and body["entity"] == "stripe"
    assert body["country"] == "DE"
    d = db_session.execute(select(Delivery)).scalar_one()
    assert d.alert_rule_id == de_rule and d.held_by_alert_id is None
    # the FR store is outside the DE segment of the organisation: no delivery
    fr = world.hosts["delta-boutique.fr"]
    _scan(db_session, fr.id, ["mollie", "stripe"], fixed_clock)
    fixed_clock.advance(days=1)
    _scan(db_session, fr.id, ["mollie", "stripe"], fixed_clock)
    r = _cycle(app_state, fixed_clock, sender)
    assert r.matched.deliveries_created == 0 and len(receiver.requests) == 1  # type: ignore[attr-defined]


def test_segment_rule_validation(
    db_session: Session, world: World, fixed_clock: FixedClock
) -> None:
    grant = _grant(db_session, world, fixed_clock)
    base = dict(
        org_id=world.org.id,
        event_types=["provider_added"],
        provider_id=None,
        method_id=None,
        min_confidence="low",
        channel="telegram",
        webhook_id=None,
        telegram_chat_id="-1",
        digest="daily",
        actor="t",
        clock=fixed_clock,
        grant=grant,
    )
    with pytest.raises(ValidationError, match="inside the entitlement segment"):
        rules.create_rule(db_session, watchlist_id=None, countries=["FR"], **base)  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="alpha-2"):
        rules.create_rule(db_session, watchlist_id=None, countries=["Germany"], **base)  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="segment rules only"):
        rules.create_rule(db_session, watchlist_id=1, countries=["DE"], **base)  # type: ignore[arg-type]
    rule = rules.create_rule(
        db_session,
        watchlist_id=None,
        countries=[" de "],
        platforms=["Shopify"],
        **base,  # type: ignore[arg-type]
    )
    assert rule.countries == ["DE"] and rule.platforms == ["shopify"]


def _removed_events(db_session: Session, world: World, provider: str, n: int, at: object) -> None:
    hosts = list(world.hosts.values())
    for i in range(n):
        host_id = hosts[i % len(hosts)].id
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
        db_session.add(
            ChangeEvent(
                host_id=host_id,
                event_type=ChangeEventType.PROVIDER_REMOVED,
                entity_type="provider",
                entity_id=provider,
                old_value=provider,
                detected_at=at,
                scan_run_id=run.id,
            )
        )
    db_session.flush()


def test_removed_spike_holds_client_alerts_until_confirmed(
    client: TestClient,
    db_session: Session,
    world: World,
    app_state: AppState,
    fixed_clock: FixedClock,
) -> None:
    hook_id = _webhook(client, world)
    _segment_rule(
        db_session,
        world,
        fixed_clock,
        webhook_id=hook_id,
        event_types=["provider_removed", "provider_added"],
    )
    receiver = Receiver()
    sender = dl.WebhookSender(
        app_state.secret_box, app_state.settings, transport=receiver.transport(), clock=fixed_clock
    )
    fixed_clock.advance(seconds=3 * 3600)
    # baseline: one stripe removal per day for a week, then 7 in the last 24 h (> 3× mean)
    for k in range(8, 1, -1):
        _removed_events(db_session, world, "stripe", 1, fixed_clock.now() - timedelta(days=k))
    _removed_events(db_session, world, "stripe", 7, fixed_clock.now() - timedelta(hours=2))
    _removed_events(db_session, world, "adyen", 2, fixed_clock.now() - timedelta(hours=2))
    spikes = anomalies.removed_spikes(db_session, now=fixed_clock.now(), multiplier=3.0)
    assert [(s.provider_id, s.count, s.baseline) for s in spikes] == [("stripe", 7, 1.0)]
    r = _cycle(app_state, fixed_clock, sender)
    assert r.anomalies == 1  # type: ignore[attr-defined]
    alert = db_session.execute(
        select(QualityAlert).where(QualityAlert.kind == anomalies.KIND_REMOVED_SPIKE)
    ).scalar_one()
    assert alert.subject == "provider:stripe" and alert.acknowledged_at is None
    # today's stripe removals on visible stores are held; adyen ones go out
    held = list(
        db_session.execute(select(Delivery).where(Delivery.held_by_alert_id == alert.id)).scalars()
    )
    assert held and all(
        d.status == DeliveryStatus.PENDING and d.next_attempt_at is None for d in held
    )
    assert r.matched.deliveries_held == len(held)  # type: ignore[attr-defined]
    sent = {json.loads(q.content)["entity"] for q in receiver.requests}
    assert sent == {"adyen"}
    # the same spike is not re-raised while open, and a second cycle sends nothing new
    r = _cycle(app_state, fixed_clock, sender)
    assert r.anomalies == 0 and r.dispatched.attempted == 0  # type: ignore[attr-defined]
    assert anomalies.held_count(db_session, alert.id) == len(held)
    # staff confirms → released → delivered on the next cycle
    released = anomalies.confirm(
        db_session, alert.id, actor="staff_analyst:a", ip=None, clock=fixed_clock
    )
    assert released == len(held)
    db_session.flush()
    r = _cycle(app_state, fixed_clock, sender)
    assert r.dispatched.delivered == len(held)  # type: ignore[attr-defined]
    assert anomalies.confirm(db_session, alert.id, actor="x", ip=None, clock=fixed_clock) == 0


def test_discarded_spike_drops_held_alerts(
    client: TestClient,
    db_session: Session,
    world: World,
    app_state: AppState,
    fixed_clock: FixedClock,
) -> None:
    hook_id = _webhook(client, world)
    _segment_rule(
        db_session, world, fixed_clock, webhook_id=hook_id, event_types=["provider_removed"]
    )
    receiver = Receiver()
    sender = dl.WebhookSender(
        app_state.secret_box, app_state.settings, transport=receiver.transport(), clock=fixed_clock
    )
    fixed_clock.advance(seconds=3600)
    _removed_events(db_session, world, "paypal", 6, fixed_clock.now() - timedelta(hours=1))
    r = _cycle(app_state, fixed_clock, sender)
    assert r.anomalies == 1 and receiver.requests == []  # type: ignore[attr-defined]
    alert = db_session.execute(select(QualityAlert)).scalar_one()
    dropped = anomalies.discard(
        db_session, alert.id, actor="staff_analyst:a", ip=None, clock=fixed_clock
    )
    # 6 events over alpha, beta, gamma, delta (FR, outside the segment), czds-only, opted-out
    assert dropped == 3
    statuses = {d.status for d in db_session.execute(select(Delivery)).scalars()}
    assert statuses == {DeliveryStatus.FAILED}
    r = _cycle(app_state, fixed_clock, sender)
    assert r.dispatched.attempted == 0 and receiver.requests == []  # type: ignore[attr-defined]


def test_portal_and_admin_pages_for_segment_rules_and_spikes(
    client: TestClient,
    db_session: Session,
    world: World,
    app_state: AppState,
    fixed_clock: FixedClock,
) -> None:
    csrf = login(client, world.org_admin_email, state=app_state, session=db_session)
    r = client.post(
        "/portal/alerts/rules",
        data={
            "csrf": csrf,
            "event_types": "provider_added",
            "countries": "DE",
            "platforms": "shopify",
            "channel": "telegram",
            "telegram_chat_id": "-100",
            "digest": "daily",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    page = client.get("/portal/alerts")
    assert "segment DE shopify" in page.text and "your segment: DE" in page.text
    r = client.post(
        "/portal/alerts/rules",
        data={
            "csrf": csrf,
            "event_types": "provider_added",
            "countries": "FR",
            "channel": "telegram",
            "telegram_chat_id": "-1",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400
    client.cookies.clear()
    # a spike alert on the admin quality page offers confirm / discard
    _removed_events(db_session, world, "klarna", 5, fixed_clock.now() - timedelta(hours=1))
    fixed_clock.advance(seconds=7200)
    created = anomalies.detect(db_session, now=fixed_clock.now(), multiplier=3.0)
    assert len(created) == 1
    csrf = login(client, world.staff_analyst_email, state=app_state, session=db_session)
    page = client.get("/admin/quality")
    assert (
        "provider_removed_spike" in page.text and "discard" in page.text and "confirm" in page.text
    )
    r = client.post(
        f"/admin/quality/alerts/{created[0].id}/discard",
        data={"csrf": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "discarded" in r.headers["location"]
    page = client.get("/admin/quality")
    assert (
        "staff_analyst" in page.text
        and "discard" not in page.text.split("provider_removed_spike")[1][:400]
    )
