"""Differ: appearance after two confirmations, removal after two misses, failed scans ignored."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.core.clock import FixedClock
from payintel.core.models.base import (
    ChangeEventType,
    ConfidenceLevel,
    DomainSourceKind,
    ProviderRole,
    ScanStatus,
    ScanType,
)
from payintel.core.models.domains import Host
from payintel.core.models.scans import ScanRun
from payintel.core.models.store import ChangeEvent, StoreCheckoutHost, StoreProvider
from payintel.core.reference_loader import load_reference, sync_reference
from payintel.discovery.ingest import ingest
from payintel.discovery.sources.base import SourceRecord
from payintel.history.differ import (
    CheckoutObservation,
    SeenHost,
    SeenMethod,
    SeenProvider,
    apply_checkout_observation,
)
from payintel.history.materialize import ProfileUpdate, upsert_profile
from payintel.history.read import read_store

T0 = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


def _host(session: Session, clock: FixedClock) -> Host:
    sync_reference(session, load_reference(), clock=clock)
    ingest(
        session,
        [SourceRecord("diff-shop.de", rank=5)],
        source=DomainSourceKind.MANUAL,
        origin="t",
        clock=clock,
    )
    return session.execute(select(Host).where(Host.hostname == "diff-shop.de")).scalar_one()


def _run(session: Session, host: Host, status: ScanStatus, when: datetime) -> uuid.UUID:
    run = ScanRun(
        id=uuid.uuid4(),
        host_id=host.id,
        scan_type=ScanType.CHECKOUT,
        started_at=when,
        finished_at=when,
        status=status,
        worker_id="t",
        ruleset_version="t",
    )
    session.add(run)
    session.flush()
    return run.id


def _obs(
    run_id: uuid.UUID,
    status: ScanStatus,
    providers: list[str],
    methods: list[str] | None = None,
    hosts: list[str] | None = None,
) -> CheckoutObservation:
    return CheckoutObservation(
        scan_run_id=run_id,
        status=status,
        providers=[
            SeenProvider(p, ProviderRole.GATEWAY, ConfidenceLevel.HIGH, 0.9) for p in providers
        ],
        methods=[SeenMethod(m, None, ConfidenceLevel.MEDIUM, 0.6) for m in (methods or [])],
        hosts=[SeenHost(h, "psp", None, 3) for h in (hosts or [])],
    )


def test_two_in_a_row_rule(db_session: Session, fixed_clock: FixedClock) -> None:
    host = _host(db_session, fixed_clock)
    upsert_profile(
        db_session,
        host.id,
        ProfileUpdate("woocommerce", ConfidenceLevel.HIGH, None, "DE", None, "EUR", None),
        scanned_at=T0,
    )
    # scan 1: stripe + paypal seen for the first time → rows, no events
    r1 = _run(db_session, host, ScanStatus.REACHED_PAYMENT_STEP, T0)
    d1 = apply_checkout_observation(
        db_session,
        host.id,
        _obs(r1, ScanStatus.REACHED_PAYMENT_STEP, ["stripe", "paypal"], ["visa"], ["stripe.com"]),
        now=T0,
    )
    assert d1.inserted == 4 and d1.events == []
    # scan 2: same → `added` events for all four entities
    r2 = _run(db_session, host, ScanStatus.REACHED_PAYMENT_STEP, T0 + timedelta(days=1))
    d2 = apply_checkout_observation(
        db_session,
        host.id,
        _obs(r2, ScanStatus.REACHED_PAYMENT_STEP, ["stripe", "paypal"], ["visa"], ["stripe.com"]),
        now=T0 + timedelta(days=1),
    )
    assert d2.by_type() == {
        "provider_added": 2,
        "method_added": 1,
        "checkout_host_added": 1,
    }
    # a blocked scan in between changes nothing
    r3 = _run(db_session, host, ScanStatus.BLOCKED, T0 + timedelta(days=2))
    d3 = apply_checkout_observation(
        db_session, host.id, _obs(r3, ScanStatus.BLOCKED, []), now=T0 + timedelta(days=2)
    )
    assert d3.ignored and d3.events == []
    # scan 4: paypal missing once → miss counted, still present
    r4 = _run(db_session, host, ScanStatus.REACHED_PAYMENT_STEP, T0 + timedelta(days=3))
    d4 = apply_checkout_observation(
        db_session,
        host.id,
        _obs(r4, ScanStatus.REACHED_PAYMENT_STEP, ["stripe"], ["visa"], ["stripe.com"]),
        now=T0 + timedelta(days=3),
    )
    assert d4.events == [] and d4.missed == 1
    paypal = db_session.execute(
        select(StoreProvider).where(
            StoreProvider.host_id == host.id, StoreProvider.provider_id == "paypal"
        )
    ).scalar_one()
    assert paypal.misses == 1
    # scan 5: paypal missing twice → removed; adyen appears (first time, no event yet)
    r5 = _run(db_session, host, ScanStatus.REACHED_PAYMENT_STEP, T0 + timedelta(days=4))
    d5 = apply_checkout_observation(
        db_session,
        host.id,
        CheckoutObservation(
            scan_run_id=r5,
            status=ScanStatus.REACHED_PAYMENT_STEP,
            providers=[
                SeenProvider("stripe", ProviderRole.GATEWAY, ConfidenceLevel.HIGH, 0.9),
                SeenProvider("adyen", ProviderRole.GATEWAY, ConfidenceLevel.MEDIUM, 0.5),
            ],
            methods=[SeenMethod("visa", "stripe", ConfidenceLevel.HIGH, 0.9)],
            hosts=[SeenHost("stripe.com", "psp", "stripe", 4)],
            platform_id="shopify",
            platform_confidence=ConfidenceLevel.HIGH,
        ),
        now=T0 + timedelta(days=4),
    )
    assert d5.by_type() == {"provider_removed": 1, "platform_changed": 1}
    assert d5.removed == 1
    ids = {
        r.provider_id
        for r in db_session.execute(
            select(StoreProvider).where(StoreProvider.host_id == host.id)
        ).scalars()
    }
    assert ids == {"stripe", "adyen"}
    # the state is readable from Postgres alone and carries the events
    state = read_store(db_session, host.id)
    assert state is not None
    assert [p["provider_id"] for p in state.providers] == ["adyen", "stripe"]
    assert state.methods[0]["provider_id"] == "stripe"
    assert state.checkout_hosts[0]["provider_id"] == "stripe"
    assert state.recent_events[0]["event_type"] in {"provider_removed", "platform_changed"}
    assert len(state.recent_events) == 6
    events = (
        db_session.execute(select(ChangeEvent).where(ChangeEvent.host_id == host.id))
        .scalars()
        .all()
    )
    assert all(not e.suppressed for e in events)
    removed = next(e for e in events if e.event_type == ChangeEventType.PROVIDER_REMOVED)
    assert removed.old_value == "paypal" and removed.scan_run_id == r5
    host_row = db_session.execute(
        select(StoreCheckoutHost).where(StoreCheckoutHost.host_id == host.id)
    ).scalar_one()
    assert host_row.confirmations == 4 and host_row.request_count == 4


def test_non_payment_outcomes_change_nothing(db_session: Session, fixed_clock: FixedClock) -> None:
    host = _host(db_session, fixed_clock)
    r1 = _run(db_session, host, ScanStatus.REACHED_PAYMENT_STEP, T0)
    apply_checkout_observation(
        db_session, host.id, _obs(r1, ScanStatus.REACHED_PAYMENT_STEP, ["mollie"]), now=T0
    )
    for status in (ScanStatus.TIMEOUT, ScanStatus.ERROR, ScanStatus.REACHED_CART):
        r = _run(db_session, host, status, T0)
        d = apply_checkout_observation(db_session, host.id, _obs(r, status, []), now=T0)
        assert d.ignored
    row = db_session.execute(
        select(StoreProvider).where(StoreProvider.host_id == host.id)
    ).scalar_one()
    assert row.misses == 0 and row.confirmations == 1
