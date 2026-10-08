"""Read the current store state from Postgres only (FR-HI-02, NFR-R-06).

ClickHouse holds the history; the profile, providers, methods and checkout
hosts a client or the admin page needs come from `store_*` alone, so they
stay readable while ClickHouse is down.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.core.models.store import (
    ChangeEvent,
    StoreCheckoutHost,
    StorePaymentMethod,
    StoreProfile,
    StoreProvider,
)


@dataclass
class StoreState:
    host_id: int
    profile: dict[str, Any]
    providers: list[dict[str, Any]] = field(default_factory=list)
    methods: list[dict[str, Any]] = field(default_factory=list)
    checkout_hosts: list[dict[str, Any]] = field(default_factory=list)
    recent_events: list[dict[str, Any]] = field(default_factory=list)


def _val(v: Any) -> Any:
    if isinstance(v, datetime | date):
        return v.isoformat()
    if hasattr(v, "value"):
        return v.value
    return v


def read_store(session: Session, host_id: int, *, events: int = 20) -> StoreState | None:
    profile = session.get(StoreProfile, host_id)
    if profile is None:
        return None
    prof = {
        "platform_id": profile.platform_id,
        "platform_confidence": _val(profile.platform_confidence),
        "platform_version": profile.platform_version,
        "country": profile.country,
        "country_confidence": _val(profile.country_confidence),
        "currency": profile.currency,
        "vertical_id": profile.vertical_id,
        "checkout_status": _val(profile.checkout_status),
        "coverage": _val(profile.coverage),
        "checkout_country": profile.checkout_country,
        "acquirer_hidden": profile.acquirer_hidden,
        "last_light_scan_at": _val(profile.last_light_scan_at),
        "last_checkout_scan_at": _val(profile.last_checkout_scan_at),
        "traffic_rank": profile.traffic_rank,
        "updated_at": _val(profile.updated_at),
    }
    providers = [
        {
            "provider_id": r.provider_id,
            "role": r.role.value,
            "confidence": r.confidence.value,
            "confidence_score": r.confidence_score,
            "active_on_checkout": r.active_on_checkout,
            "first_seen": r.first_seen.isoformat(),
            "last_seen": r.last_seen.isoformat(),
            "confirmations": r.confirmations,
            "misses": r.misses,
        }
        for r in session.execute(
            select(StoreProvider)
            .where(StoreProvider.host_id == host_id)
            .order_by(StoreProvider.provider_id)
        ).scalars()
    ]
    methods = [
        {
            "method_id": r.method_id,
            "provider_id": r.provider_id,
            "confidence": r.confidence.value,
            "confidence_score": r.confidence_score,
            "first_seen": r.first_seen.isoformat(),
            "last_seen": r.last_seen.isoformat(),
            "confirmations": r.confirmations,
            "misses": r.misses,
        }
        for r in session.execute(
            select(StorePaymentMethod)
            .where(StorePaymentMethod.host_id == host_id)
            .order_by(StorePaymentMethod.method_id)
        ).scalars()
    ]
    hosts = [
        {
            "third_party_etld1": r.third_party_etld1,
            "category": r.category,
            "provider_id": r.provider_id,
            "request_count": r.request_count,
            "first_seen": r.first_seen.isoformat(),
            "last_seen": r.last_seen.isoformat(),
            "confirmations": r.confirmations,
            "misses": r.misses,
        }
        for r in session.execute(
            select(StoreCheckoutHost)
            .where(StoreCheckoutHost.host_id == host_id)
            .order_by(StoreCheckoutHost.third_party_etld1)
        ).scalars()
    ]
    recent = [
        {
            "event_type": e.event_type.value,
            "entity_type": e.entity_type,
            "entity_id": e.entity_id,
            "old_value": e.old_value,
            "new_value": e.new_value,
            "detected_at": e.detected_at.isoformat(),
            "scan_run_id": str(e.scan_run_id),
        }
        for e in session.execute(
            select(ChangeEvent)
            .where(ChangeEvent.host_id == host_id)
            .order_by(ChangeEvent.detected_at.desc(), ChangeEvent.id.desc())
            .limit(events)
        ).scalars()
    ]
    return StoreState(host_id, prof, providers, methods, hosts, recent)
