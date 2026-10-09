"""Match new change events to alert rules and create deliveries (FR-AL-01…04, AC-09).

A rule fires for an event when:
* the store's domain is in the rule's watchlist, or — for a segment rule
  without a watchlist (FR-AL-05) — the store is in the rule's `countries` /
  `platforms` filter (empty = the whole entitlement segment);
* the event type is in the rule;
* the provider / method filter (if any) equals the event entity;
* the entity's current confidence is at least `min_confidence` (for removals
  the confidence of the last observation is unknown → treated as `high`);
* the store is inside the organisation's segment and visible to C1
  (lineage guard: opted-out or CZDS-only stores never produce alerts).

Deliveries are idempotent per (rule, event); the cursor table records the
last processed event id. While a `provider_removed_spike` quality alert is
open for a provider (FR-QA-04), deliveries about that provider are created
*held* (`held_by_alert_id`, no `next_attempt_at`) and go out only once an
analyst confirms the alert.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.core.clock import Clock
from payintel.core.flags import FlagService
from payintel.core.models.alerts import AlertRule, Delivery, Watchlist, WatchlistItem
from payintel.core.models.base import DeliveryStatus
from payintel.core.models.domains import Domain, Host
from payintel.core.models.portal import SystemCursor
from payintel.core.models.store import ChangeEvent, StorePaymentMethod, StoreProfile, StoreProvider
from payintel.core.settings import Settings
from payintel.entitlements.check import EntitlementDenied, resolve_grant
from payintel.entitlements.lineage_guard import c1_visible_clause
from payintel.entitlements.segment import in_segment
from payintel.quality import anomalies

CURSOR_KEY = "alerts.last_change_event_id"
_RANK = {"low": 1, "medium": 2, "high": 3}


@dataclass(frozen=True)
class MatchResult:
    events_seen: int
    deliveries_created: int
    last_event_id: int
    deliveries_held: int = 0


def _cursor(session: Session) -> int:
    row = session.get(SystemCursor, CURSOR_KEY)
    return row.value if row else 0


def _set_cursor(session: Session, value: int, now: datetime) -> None:
    row = session.get(SystemCursor, CURSOR_KEY)
    if row is None:
        session.add(SystemCursor(key=CURSOR_KEY, value=value, updated_at=now))
    else:
        row.value = value
        row.updated_at = now
    session.flush()


def _confidence_of(session: Session, event: ChangeEvent) -> str:
    if event.entity_type == "provider":
        row = session.execute(
            select(StoreProvider.confidence).where(
                StoreProvider.host_id == event.host_id, StoreProvider.provider_id == event.entity_id
            )
        ).scalar_one_or_none()
    elif event.entity_type == "payment_method":
        row = session.execute(
            select(StorePaymentMethod.confidence).where(
                StorePaymentMethod.host_id == event.host_id,
                StorePaymentMethod.method_id == event.entity_id,
            )
        ).scalar_one_or_none()
    else:
        row = None
    return row.value if row is not None else "high"


def payload_for(event: ChangeEvent, domain: str, profile: StoreProfile | None) -> dict[str, Any]:
    return {
        "event_id": event.id,
        "domain": domain,
        "type": event.event_type.value,
        "entity_type": event.entity_type,
        "entity": event.entity_id,
        "old_value": event.old_value,
        "new_value": event.new_value,
        "detected_at": event.detected_at.isoformat(),
        "country": profile.country if profile else None,
        "platform_id": profile.platform_id if profile else None,
    }


def match_new_events(
    session: Session, *, settings: Settings, clock: Clock, limit: int = 1_000
) -> MatchResult:
    now = clock.now()
    last = _cursor(session)
    events = list(
        session.execute(
            select(ChangeEvent, Domain.etld1, StoreProfile)
            .join(Host, Host.id == ChangeEvent.host_id)
            .join(Domain, Domain.id == Host.domain_id)
            .outerjoin(StoreProfile, StoreProfile.host_id == ChangeEvent.host_id)
            .where(
                ChangeEvent.id > last,
                ChangeEvent.suppressed.is_(False),
                c1_visible_clause(events=True),
            )
            .order_by(ChangeEvent.id)
            .limit(limit)
        ).all()
    )
    max_id = max((e.id for e, _, _ in events), default=last)
    # Also advance past suppressed / invisible events so they are not re-read forever.
    tail = session.execute(
        select(ChangeEvent.id).where(ChangeEvent.id > last).order_by(ChangeEvent.id.desc()).limit(1)
    ).scalar_one_or_none()
    if tail is not None and len(events) < limit:
        max_id = max(max_id, tail)
    if not events:
        _set_cursor(session, max_id, now)
        return MatchResult(0, 0, max_id)

    rules = list(session.execute(select(AlertRule).where(AlertRule.enabled.is_(True))).scalars())
    flags = FlagService(session, settings.flags, clock=clock)
    grants: dict[uuid.UUID, Any] = {}
    domains = {etld1 for _, etld1, _ in events}
    members = session.execute(
        select(WatchlistItem.domain, Watchlist.id, Watchlist.org_id)
        .join(Watchlist, Watchlist.id == WatchlistItem.watchlist_id)
        .where(WatchlistItem.domain.in_(sorted(domains)))
    ).all()
    by_domain: dict[str, set[tuple[int, uuid.UUID]]] = {}
    for d, wl_id, org_id in members:
        by_domain.setdefault(d, set()).add((wl_id, org_id))

    holds = anomalies.open_spike_alerts(session)
    created = held = 0
    for event, etld1, profile in events:
        lists = by_domain.get(etld1, set())
        confidence: str | None = None
        for rule in rules:
            if event.detected_at < rule.created_at:
                continue  # rules do not replay history that predates them
            if event.event_type.value not in rule.event_types:
                continue
            if rule.watchlist_id is not None:
                if (rule.watchlist_id, rule.org_id) not in lists:
                    continue
            else:  # FR-AL-05: segment subscription, optional country/platform narrowing
                country = (profile.country if profile else None) or ""
                platform = (profile.platform_id if profile else None) or ""
                if rule.countries and country.upper() not in rule.countries:
                    continue
                if rule.platforms and platform not in rule.platforms:
                    continue
            if confidence is None:
                confidence = _confidence_of(session, event)
            if rule.provider_id and not (
                event.entity_type == "provider" and event.entity_id == rule.provider_id
            ):
                continue
            if rule.method_id and not (
                event.entity_type == "payment_method" and event.entity_id == rule.method_id
            ):
                continue
            if _RANK[confidence] < _RANK.get(rule.min_confidence, 2):
                continue
            if rule.org_id not in grants:
                try:
                    grants[rule.org_id] = resolve_grant(
                        session, rule.org_id, today=now.date(), flags=flags
                    )
                except EntitlementDenied:
                    grants[rule.org_id] = None
            grant = grants[rule.org_id]
            if grant is None:
                continue
            if profile is not None and not in_segment(
                grant, country=profile.country, platform_id=profile.platform_id
            ):
                continue
            exists = session.execute(
                select(Delivery.id).where(
                    Delivery.alert_rule_id == rule.id, Delivery.change_event_id == event.id
                )
            ).first()
            if exists:
                continue
            hold_id = holds.get(event.entity_id) if event.entity_type == "provider" else None
            session.add(
                Delivery(
                    alert_rule_id=rule.id,
                    change_event_id=event.id,
                    channel=rule.channel,
                    payload=payload_for(event, etld1, profile),
                    status=DeliveryStatus.PENDING,
                    next_attempt_at=None if hold_id is not None else now,
                    held_by_alert_id=hold_id,
                    created_at=now,
                )
            )
            created += 1
            held += hold_id is not None
    session.flush()
    _set_cursor(session, max_id, now)
    return MatchResult(len(events), created, max_id, held)
