"""FR-QA-06 stop review and FR-QA-03 dashboard on real ClickHouse + PostgreSQL rows."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from clickhouse_connect.driver.client import Client
from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.core.clock import FixedClock
from payintel.core.models.base import (
    ChangeEventType,
    ConfidenceLevel,
    DomainSourceKind,
    ProviderRole,
)
from payintel.core.models.domains import Host
from payintel.core.models.quality import QualityAlert
from payintel.core.models.store import ChangeEvent, StoreProfile, StoreProvider
from payintel.core.reference_loader import load_reference, sync_reference
from payintel.core.settings import QualitySettings
from payintel.crawl.checkout.stops import StopContext, stop_row
from payintel.crawl.checkout.types import StepTiming, Stop, WalkStep
from payintel.discovery.ingest import ingest
from payintel.discovery.sources.base import SourceRecord
from payintel.history.writer import ObservationBuffer
from payintel.quality import dashboard as dash
from payintel.quality import stops as stops_mod

STEP_OF = {
    "checkout_not_found": WalkStep.CHECKOUT,
    "no_product_found": WalkStep.PRODUCT,
    "other": WalkStep.NAVIGATION,
    "http_403_429": WalkStep.PROTECTION,
}


def _stops(
    buf: ObservationBuffer,
    *,
    when: datetime,
    platform: str,
    version: str,
    reasons: dict[str, int],
    start_host: int = 1,
) -> None:
    host_id = start_host
    for reason, n in reasons.items():
        for i in range(n):
            run_id = uuid.uuid4()
            prefix = f"checkout/shop{host_id}.de/{run_id}/"
            ctx = StopContext(
                scan_run_id=run_id,
                host_id=host_id,
                etld1=f"shop{host_id}.de",
                platform_id=platform,
                adapter=platform,
                scanner_version=version,
                ruleset_version="r1",
                artifact_prefix=prefix,
            )
            stop = Stop(
                STEP_OF[reason], reason, f"detail {i}" if reason == "other" else "", page_url="u"
            )
            buf.add(
                "obs_scan_stop",
                stop_row(
                    stop,
                    ctx,
                    steps=[StepTiming("navigation", 100)],
                    now=when - timedelta(minutes=i),
                    artifact_key=f"{prefix}stop.jpg",
                ),
            )
            host_id += 1


def test_stop_review_sample_csv_and_alerts(
    db_session: Session, fixed_clock: FixedClock, ch_client: Client
) -> None:
    now = fixed_clock.now()
    buf = ObservationBuffer(ch_client)
    # previous week, scanner 0.1.0: checkout_not_found 33 %
    _stops(
        buf,
        when=now - timedelta(days=9),
        platform="woocommerce",
        version="0.1.0",
        reasons={"checkout_not_found": 10, "no_product_found": 18, "other": 2},
    )
    # this week, scanner 0.2.0: checkout_not_found 67 %, other 6.7 %
    _stops(
        buf,
        when=now - timedelta(days=2),
        platform="woocommerce",
        version="0.2.0",
        reasons={"checkout_not_found": 20, "no_product_found": 8, "other": 2},
        start_host=100,
    )
    # too few stops on magento2 for an alert
    _stops(
        buf,
        when=now - timedelta(days=1),
        platform="magento2",
        version="0.2.0",
        reasons={"http_403_429": 3},
        start_host=500,
    )
    buf.flush()

    r = stops_mod.review(ch_client, now=now, days=7)
    assert r.total == 33 and r.by_reason["checkout_not_found"] == 20
    assert r.by_step == {"checkout": 20, "product": 8, "protection": 3, "navigation": 2}
    assert r.by_platform == {"woocommerce": 30, "magento2": 3}
    assert abs(r.other_share - 2 / 33) < 1e-6
    assert [rel.version for rel in r.releases] == ["0.1.0", "0.2.0"]
    assert r.releases[1].shares["checkout_not_found"] == round(20 / 33, 4)
    assert r.weekly and sum(w.count for w in r.weekly) == 63
    lines = stops_mod.summary_lines(r)
    assert lines[0].startswith("stops ") and "release 0.2.0" in lines[-1]
    assert r.as_dict()["total"] == 33

    rows = stops_mod.sample(
        ch_client,
        step="checkout",
        reason="checkout_not_found",
        since=now - timedelta(days=7),
        until=now,
        platform_id="woocommerce",
    )
    assert len(rows) == 20 and len({s.etld1 for s in rows}) == 20
    assert all(
        s.screenshot_key.endswith("stop.jpg") and s.dom_key.endswith("stop.html.gz") for s in rows
    )
    assert rows[0].scan_ts >= rows[-1].scan_ts
    text = stops_mod.sample_csv(rows)
    assert (
        text.splitlines()[0].startswith("scan_ts,etld1,platform_id")
        and len(text.splitlines()) == 21
    )
    assert (
        stops_mod.sample(
            ch_client,
            step="checkout",
            reason="checkout_not_found",
            since=now - timedelta(days=7),
            until=now,
            limit=5,
        ).__len__()
        == 5
    )

    s = QualitySettings()
    created = stops_mod.detect_alerts(db_session, ch_client, now=now, s=s)
    kinds = {(a.kind, a.subject) for a in created}
    assert (stops_mod.KIND_GROWTH, "woocommerce:checkout_not_found") in kinds
    assert (stops_mod.KIND_GROWTH, "release:0.2.0:checkout_not_found") in kinds
    assert (stops_mod.KIND_OTHER, "all") in kinds
    assert not any(a.subject.startswith("magento2") for a in created)
    growth = next(a for a in created if a.subject == "woocommerce:checkout_not_found")
    assert growth.baseline == round(10 / 30, 4) and growth.value == round(20 / 30, 4)
    assert growth.threshold == 5.0 and growth.window_days == 7
    # idempotent while the alerts stay open
    assert stops_mod.detect_alerts(db_session, ch_client, now=now, s=s) == []
    stored = db_session.execute(select(QualityAlert)).scalars().all()
    assert len(stored) == len(created) and all(a.acknowledged_at is None for a in stored)


def _host(session: Session, clock: FixedClock, name: str) -> Host:
    ingest(session, [SourceRecord(name)], source=DomainSourceKind.MANUAL, origin="t", clock=clock)
    return session.execute(select(Host).where(Host.hostname == name)).scalar_one()


def test_dashboard_outcomes_confidence_freshness_and_spikes(
    db_session: Session, fixed_clock: FixedClock, ch_client: Client
) -> None:
    now = fixed_clock.now()
    sync_reference(db_session, load_reference(), clock=fixed_clock)
    buf = ObservationBuffer(ch_client)

    def scan(platform: str, status: str, i: int) -> None:
        buf.add(
            "obs_scan",
            {
                "scan_date": now.date(),
                "scan_ts": now - timedelta(hours=i),
                "scan_run_id": uuid.uuid4(),
                "host_id": i,
                "etld1": f"s{i}.de",
                "scan_type": "checkout",
                "status": status,
                "coverage": "cart",
                "duration_ms": 1000,
                "blocked_by": "",
                "platform_id": platform,
                "adapter": platform,
                "scanner_version": "0.2.0",
                "ruleset_version": "r1",
            },
        )

    i = 0
    for status, n in (("reached_payment_step", 6), ("reached_checkout", 2), ("blocked", 2)):
        for _ in range(n):
            i += 1
            scan("woocommerce", status, i)
    for status, n in (("reached_payment_step", 1), ("blocked", 3)):
        for _ in range(n):
            i += 1
            scan("magento2", status, i)
    buf.add(
        "obs_scan",
        {
            "scan_date": now.date(),
            "scan_ts": now,
            "scan_run_id": uuid.uuid4(),
            "host_id": 1,
            "etld1": "s1.de",
            "scan_type": "light",
            "status": "ok",
            "coverage": "cart",
            "duration_ms": 10,
            "blocked_by": "",
            "platform_id": "woocommerce",
            "adapter": "light",
            "scanner_version": "0.2.0",
            "ruleset_version": "r1",
        },
    )
    buf.flush()

    h1 = _host(db_session, fixed_clock, "fresh.de")
    h2 = _host(db_session, fixed_clock, "stale.de")
    h3 = _host(db_session, fixed_clock, "never.de")
    db_session.add_all(
        [
            StoreProfile(
                host_id=h1.id,
                last_light_scan_at=now - timedelta(days=1),
                last_checkout_scan_at=now - timedelta(days=10),
                updated_at=now,
            ),
            StoreProfile(
                host_id=h2.id,
                last_light_scan_at=now - timedelta(days=20),
                last_checkout_scan_at=now - timedelta(days=40),
                updated_at=now,
            ),
            StoreProfile(host_id=h3.id, updated_at=now),
        ]
    )
    for pid, conf in (
        ("stripe", ConfidenceLevel.HIGH),
        ("paypal", ConfidenceLevel.HIGH),
        ("klarna", ConfidenceLevel.LOW),
    ):
        db_session.add(
            StoreProvider(
                host_id=h1.id,
                provider_id=pid,
                role=ProviderRole.GATEWAY,
                confidence=conf,
                confidence_score=0.9,
                active_on_checkout=True,
                first_seen=now.date(),
                last_seen=now.date(),
            )
        )
    run_id = uuid.uuid4()
    from payintel.core.models.scans import ScanRun

    db_session.add(
        ScanRun(
            id=run_id,
            host_id=h1.id,
            scan_type="checkout",
            started_at=now,
            finished_at=now,
            status="reached_payment_step",
            worker_id="t",
            ruleset_version="r1",
        )
    )
    db_session.flush()
    for day_back, count in ((1, 1), (2, 1), (3, 1), (4, 2), (5, 1), (6, 1), (7, 1), (0, 15)):
        for _ in range(count):
            db_session.add(
                ChangeEvent(
                    host_id=h1.id,
                    event_type=ChangeEventType.PROVIDER_ADDED,
                    entity_type="provider",
                    entity_id="stripe",
                    detected_at=now - timedelta(days=day_back, hours=1),
                    scan_run_id=run_id,
                )
            )
    db_session.flush()

    d = dash.dashboard(db_session, ch_client, now=now, days=7)
    by = {o.platform_id: o for o in d.outcomes}
    assert by["woocommerce"].walks == 10 and by["woocommerce"].to_checkout == 0.8
    assert by["woocommerce"].to_payment == 0.6 and by["woocommerce"].blocked == 0.2
    assert by["magento2"].walks == 4 and by["magento2"].blocked == 0.75
    assert d.overall is not None and d.overall.walks == 14 and d.overall.to_payment == 0.5
    assert d.provider_confidence == {"high": 2, "low": 1} and d.method_confidence == {}
    f = d.freshness
    assert f.profiles == 3 and f.light_never == 1 and f.checkout_never == 1
    assert f.light_median_days == 10.5 and f.light_stale_share == 0.5
    assert f.checkout_median_days == 25.0 and f.checkout_stale_share == 0.5
    assert len(d.events_per_day) == 7
    assert [e.day.date() for e in d.spikes] == [now.date()]
    assert d.events_per_day[-1].count == 15
    assert d.as_dict()["spikes"] == [now.date().isoformat()]
    lines = dash.summary_lines(d)
    assert any("to payment step 50.0%" in line for line in lines)
    assert lines[-1].endswith("15!")
