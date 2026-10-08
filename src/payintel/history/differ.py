"""Change detection with the "two in a row" rule (FR-HI-03, FR-HI-04).

A checkout scan reports what it saw on the payment step. The differ keeps
the `store_*` rows in step with it and emits `change_event` rows:

- an entity (provider, payment method, third-party checkout host) that is
  seen for the first time gets a row with `confirmations = 1`; the event
  `*_added` is written when the *second* consecutive scan confirms it;
- an entity missing from a scan gets `misses += 1`; the event `*_removed`
  is written when it is missing from two consecutive scans, and the row is
  dropped (the history stays in ClickHouse);
- scans that ended `blocked`, `timeout` or `error`, or that did not reach the
  payment step, change nothing: nothing was observed, so nothing is missing;
- `platform_changed` fires when a medium/high platform finding differs from
  the profile's platform.

Rows written by light scans (low confidence, never active on checkout) are
confirmed or missed by the same counters: a provider that a checkout scan
no longer sees twice in a row is gone from the current state whatever found
it first.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.core.models.base import ChangeEventType, ConfidenceLevel, ProviderRole, ScanStatus
from payintel.core.models.store import (
    ChangeEvent,
    StoreCheckoutHost,
    StorePaymentMethod,
    StoreProfile,
    StoreProvider,
)
from payintel.history.materialize import _RANK

CONFIRMATIONS_FOR_ADD = 2
MISSES_FOR_REMOVE = 2
IGNORED_STATUSES = frozenset({ScanStatus.BLOCKED, ScanStatus.TIMEOUT, ScanStatus.ERROR})


@dataclass(frozen=True)
class SeenProvider:
    provider_id: str
    role: ProviderRole
    confidence: ConfidenceLevel
    score: float
    active_on_checkout: bool = True


@dataclass(frozen=True)
class SeenMethod:
    method_id: str
    provider_id: str | None
    confidence: ConfidenceLevel
    score: float


@dataclass(frozen=True)
class SeenHost:
    etld1: str
    category: str
    provider_id: str | None
    request_count: int


@dataclass(frozen=True)
class CheckoutObservation:
    scan_run_id: uuid.UUID
    status: ScanStatus
    providers: list[SeenProvider] = field(default_factory=list)
    methods: list[SeenMethod] = field(default_factory=list)
    hosts: list[SeenHost] = field(default_factory=list)
    platform_id: str | None = None
    platform_confidence: ConfidenceLevel | None = None


@dataclass
class DiffResult:
    events: list[ChangeEvent] = field(default_factory=list)
    ignored: bool = False
    inserted: int = 0
    confirmed: int = 0
    missed: int = 0
    removed: int = 0

    def by_type(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self.events:
            out[e.event_type.value] = out.get(e.event_type.value, 0) + 1
        return out


def apply_checkout_observation(
    session: Session, host_id: int, obs: CheckoutObservation, *, now: datetime
) -> DiffResult:
    """Materialise the observation and write change events; returns what changed."""
    result = DiffResult()
    if obs.status in IGNORED_STATUSES or obs.status != ScanStatus.REACHED_PAYMENT_STEP:
        result.ignored = True
        return result
    today = now.date()

    def event(
        kind: ChangeEventType, entity_type: str, entity_id: str, old: str | None, new: str | None
    ) -> None:
        ev = ChangeEvent(
            host_id=host_id,
            event_type=kind,
            entity_type=entity_type,
            entity_id=entity_id,
            old_value=old,
            new_value=new,
            detected_at=now,
            scan_run_id=obs.scan_run_id,
        )
        session.add(ev)
        result.events.append(ev)

    # --- providers ---------------------------------------------------------------------
    rows = {
        r.provider_id: r
        for r in session.execute(
            select(StoreProvider).where(StoreProvider.host_id == host_id)
        ).scalars()
    }
    seen_ids = {p.provider_id for p in obs.providers}
    for p in obs.providers:
        row = rows.get(p.provider_id)
        if row is None:
            session.add(
                StoreProvider(
                    host_id=host_id,
                    provider_id=p.provider_id,
                    role=p.role,
                    confidence=p.confidence,
                    confidence_score=p.score,
                    active_on_checkout=p.active_on_checkout,
                    first_seen=today,
                    last_seen=today,
                    confirmations=1,
                    misses=0,
                )
            )
            result.inserted += 1
            continue
        row.last_seen = today
        row.confirmations += 1
        row.misses = 0
        if _RANK[p.confidence] >= _RANK[row.confidence]:
            row.confidence = p.confidence
            row.confidence_score = max(row.confidence_score, p.score)
        row.active_on_checkout = row.active_on_checkout or p.active_on_checkout
        result.confirmed += 1
        if row.confirmations == CONFIRMATIONS_FOR_ADD:
            event(ChangeEventType.PROVIDER_ADDED, "provider", p.provider_id, None, p.provider_id)
    for pid, row in rows.items():
        if pid in seen_ids:
            continue
        row.misses += 1
        result.missed += 1
        if row.misses >= MISSES_FOR_REMOVE:
            event(ChangeEventType.PROVIDER_REMOVED, "provider", pid, pid, None)
            session.delete(row)
            result.removed += 1

    # --- payment methods ---------------------------------------------------------------
    mrows = {
        r.method_id: r
        for r in session.execute(
            select(StorePaymentMethod).where(StorePaymentMethod.host_id == host_id)
        ).scalars()
    }
    seen_methods = {m.method_id for m in obs.methods}
    for m in obs.methods:
        row2 = mrows.get(m.method_id)
        if row2 is None:
            session.add(
                StorePaymentMethod(
                    host_id=host_id,
                    method_id=m.method_id,
                    provider_id=m.provider_id,
                    confidence=m.confidence,
                    confidence_score=m.score,
                    first_seen=today,
                    last_seen=today,
                    confirmations=1,
                    misses=0,
                )
            )
            result.inserted += 1
            continue
        row2.last_seen = today
        row2.confirmations += 1
        row2.misses = 0
        if m.provider_id:
            row2.provider_id = m.provider_id
        if _RANK[m.confidence] >= _RANK[row2.confidence]:
            row2.confidence = m.confidence
            row2.confidence_score = max(row2.confidence_score, m.score)
        result.confirmed += 1
        if row2.confirmations == CONFIRMATIONS_FOR_ADD:
            event(ChangeEventType.METHOD_ADDED, "payment_method", m.method_id, None, m.method_id)
    for mid, row2 in mrows.items():
        if mid in seen_methods:
            continue
        row2.misses += 1
        result.missed += 1
        if row2.misses >= MISSES_FOR_REMOVE:
            event(ChangeEventType.METHOD_REMOVED, "payment_method", mid, mid, None)
            session.delete(row2)
            result.removed += 1

    # --- third-party checkout hosts ----------------------------------------------------
    hrows = {
        r.third_party_etld1: r
        for r in session.execute(
            select(StoreCheckoutHost).where(StoreCheckoutHost.host_id == host_id)
        ).scalars()
    }
    seen_hosts = {h.etld1 for h in obs.hosts}
    for h in obs.hosts:
        row3 = hrows.get(h.etld1)
        if row3 is None:
            session.add(
                StoreCheckoutHost(
                    host_id=host_id,
                    third_party_etld1=h.etld1,
                    category=h.category,
                    provider_id=h.provider_id,
                    request_count=h.request_count,
                    first_seen=today,
                    last_seen=today,
                    confirmations=1,
                    misses=0,
                )
            )
            result.inserted += 1
            continue
        row3.last_seen = today
        row3.confirmations += 1
        row3.misses = 0
        row3.category = h.category
        row3.provider_id = h.provider_id
        row3.request_count = h.request_count
        result.confirmed += 1
        if row3.confirmations == CONFIRMATIONS_FOR_ADD:
            event(ChangeEventType.CHECKOUT_HOST_ADDED, "checkout_host", h.etld1, None, h.category)
    for etld1, row3 in hrows.items():
        if etld1 in seen_hosts:
            continue
        row3.misses += 1
        result.missed += 1
        if row3.misses >= MISSES_FOR_REMOVE:
            event(
                ChangeEventType.CHECKOUT_HOST_REMOVED, "checkout_host", etld1, row3.category, None
            )
            session.delete(row3)
            result.removed += 1

    # --- platform ----------------------------------------------------------------------
    if obs.platform_id and obs.platform_confidence in {
        ConfidenceLevel.MEDIUM,
        ConfidenceLevel.HIGH,
    }:
        profile = session.get(StoreProfile, host_id)
        if profile is not None and profile.platform_id and profile.platform_id != obs.platform_id:
            event(
                ChangeEventType.PLATFORM_CHANGED,
                "platform",
                obs.platform_id,
                profile.platform_id,
                obs.platform_id,
            )
    session.flush()
    return result
