"""FR-QA-04: anomalies in `provider_removed` events.

When the number of `provider_removed` events of one provider in the last 24 h
exceeds `anomaly_removed_multiplier` × its daily mean over the previous 7 days
(and at least `min_events`), a `provider_removed_spike` quality alert is raised
for `staff_analyst` and every client delivery about that provider is *held*
(`delivery.held_by_alert_id`, no `next_attempt_at`) until an analyst confirms
the alert. Confirming releases the deliveries; discarding marks them failed
with a note, so the journal keeps the trace.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Row, func, select, update
from sqlalchemy.orm import Session

from payintel.core import audit
from payintel.core.clock import Clock
from payintel.core.errors import NotFoundError
from payintel.core.models.alerts import Delivery
from payintel.core.models.base import ChangeEventType, DeliveryStatus
from payintel.core.models.quality import QualityAlert
from payintel.core.models.store import ChangeEvent

KIND_REMOVED_SPIKE = "provider_removed_spike"
MIN_EVENTS = 5
BASELINE_DAYS = 7


@dataclass(frozen=True)
class RemovedSpike:
    provider_id: str
    count: int
    baseline: float


def _counts(rows: Sequence[Row[tuple[str | None, int]]]) -> dict[str, int]:
    return {str(pid): int(n) for pid, n in rows if pid is not None}


def removed_spikes(
    session: Session, *, now: datetime, multiplier: float, min_events: int = MIN_EVENTS
) -> list[RemovedSpike]:
    """Providers whose last-24 h removals exceed `multiplier` × the 7-day daily mean."""
    day_ago = now - timedelta(days=1)
    base_start = day_ago - timedelta(days=BASELINE_DAYS)
    recent: dict[str, int] = _counts(
        session.execute(
            select(ChangeEvent.entity_id, func.count())
            .where(
                ChangeEvent.event_type == ChangeEventType.PROVIDER_REMOVED,
                ChangeEvent.entity_type == "provider",
                ChangeEvent.detected_at >= day_ago,
                ChangeEvent.detected_at < now,
            )
            .group_by(ChangeEvent.entity_id)
        ).all()
    )
    baseline: dict[str, int] = _counts(
        session.execute(
            select(ChangeEvent.entity_id, func.count())
            .where(
                ChangeEvent.event_type == ChangeEventType.PROVIDER_REMOVED,
                ChangeEvent.entity_type == "provider",
                ChangeEvent.detected_at >= base_start,
                ChangeEvent.detected_at < day_ago,
            )
            .group_by(ChangeEvent.entity_id)
        ).all()
    )
    out = []
    for provider_id, count in sorted(recent.items()):
        mean = int(baseline.get(provider_id, 0)) / BASELINE_DAYS
        if count >= min_events and count > multiplier * max(mean, 1.0):
            out.append(RemovedSpike(str(provider_id), int(count), round(mean, 3)))
    return out


def open_spike_alerts(session: Session) -> dict[str, int]:
    """provider_id → alert id for every unacknowledged `provider_removed_spike`."""
    rows = session.execute(
        select(QualityAlert.id, QualityAlert.subject).where(
            QualityAlert.kind == KIND_REMOVED_SPIKE, QualityAlert.acknowledged_at.is_(None)
        )
    ).all()
    return {str(subject).removeprefix("provider:"): int(aid) for aid, subject in rows}


def detect(
    session: Session, *, now: datetime, multiplier: float, min_events: int = MIN_EVENTS
) -> list[QualityAlert]:
    """Raise one alert per provider in spike (idempotent while the alert is open)."""
    already = open_spike_alerts(session)
    created = []
    for spike in removed_spikes(session, now=now, multiplier=multiplier, min_events=min_events):
        if spike.provider_id in already:
            continue
        alert = QualityAlert(
            kind=KIND_REMOVED_SPIKE,
            subject=f"provider:{spike.provider_id}",
            value=float(spike.count),
            baseline=spike.baseline,
            threshold=multiplier,
            window_days=BASELINE_DAYS,
            message=(
                f"{spike.count} provider_removed events for {spike.provider_id} in 24 h "
                f"vs {spike.baseline:.2f}/day over the previous {BASELINE_DAYS} days "
                f"(> ×{multiplier:g}); client alerts for this provider are held until confirmed"
            ),
            detected_at=now,
        )
        session.add(alert)
        created.append(alert)
    session.flush()
    return created


def held_count(session: Session, alert_id: int) -> int:
    return int(
        session.execute(
            select(func.count())
            .select_from(Delivery)
            .where(Delivery.held_by_alert_id == alert_id, Delivery.status == DeliveryStatus.PENDING)
        ).scalar_one()
    )


def confirm(session: Session, alert_id: int, *, actor: str, ip: str | None, clock: Clock) -> int:
    """Acknowledge the alert and release its held deliveries (they go out next cycle)."""
    alert = session.get(QualityAlert, alert_id)
    if alert is None:
        raise NotFoundError("alert not found")
    now = clock.now()
    released = 0
    if alert.acknowledged_at is None:
        alert.acknowledged_at = now
        alert.acknowledged_by = actor
        result = session.execute(
            update(Delivery)
            .where(
                Delivery.held_by_alert_id == alert_id,
                Delivery.status == DeliveryStatus.PENDING,
                Delivery.next_attempt_at.is_(None),
            )
            .values(next_attempt_at=now)
        )
        released = int(getattr(result, "rowcount", 0) or 0)
        session.flush()
        audit.record(
            session,
            actor=actor,
            action="quality_alert.ack",
            object_type="quality_alert",
            object_id=str(alert.id),
            after={"kind": alert.kind, "released_deliveries": released},
            ip=ip,
            clock=clock,
        )
    return released


def discard(session: Session, alert_id: int, *, actor: str, ip: str | None, clock: Clock) -> int:
    """Acknowledge the alert as a false detection: held deliveries are never sent."""
    alert = session.get(QualityAlert, alert_id)
    if alert is None:
        raise NotFoundError("alert not found")
    now = clock.now()
    dropped = 0
    if alert.acknowledged_at is None:
        alert.acknowledged_at = now
        alert.acknowledged_by = actor
        result = session.execute(
            update(Delivery)
            .where(
                Delivery.held_by_alert_id == alert_id,
                Delivery.status == DeliveryStatus.PENDING,
            )
            .values(
                status=DeliveryStatus.FAILED,
                next_attempt_at=None,
                last_error="discarded after anomaly review (FR-QA-04)",
            )
        )
        dropped = int(getattr(result, "rowcount", 0) or 0)
        session.flush()
        audit.record(
            session,
            actor=actor,
            action="quality_alert.discard",
            object_type="quality_alert",
            object_id=str(alert.id),
            after={"kind": alert.kind, "discarded_deliveries": dropped},
            ip=ip,
            clock=clock,
        )
    return dropped
