"""`store_offline` / `store_online` change events (FR-SC-05, FR-HI-03).

A store goes *offline* when its retry budget is exhausted and the domain is
marked `unreachable` while it was an e-commerce site; it comes back *online*
when a later successful light scan classifies it as e-commerce again. Both are
ordinary change events (entity `store`, id = eTLD+1), so they flow through the
history, the API `changes` feed and the alert rules.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.core.models.base import ChangeEventType, DomainStatus
from payintel.core.models.domains import Domain, Host
from payintel.core.models.store import ChangeEvent, StoreProfile

ENTITY_TYPE = "store"


def _event(
    session: Session,
    host_id: int,
    kind: ChangeEventType,
    etld1: str,
    old: DomainStatus,
    new: DomainStatus,
    scan_run_id: uuid.UUID,
    now: datetime,
) -> ChangeEvent:
    ev = ChangeEvent(
        host_id=host_id,
        event_type=kind,
        entity_type=ENTITY_TYPE,
        entity_id=etld1,
        old_value=old.value,
        new_value=new.value,
        detected_at=now,
        scan_run_id=scan_run_id,
    )
    session.add(ev)
    session.flush()
    return ev


def mark_offline(
    session: Session, host: Host, domain: Domain, *, scan_run_id: uuid.UUID | None, now: datetime
) -> ChangeEvent | None:
    """Called when the retry budget is exhausted; emits `store_offline` for e-commerce stores."""
    was = domain.status
    if was == DomainStatus.OPTOUT:
        return None
    domain.status = DomainStatus.UNREACHABLE
    domain.status_reason = "retry_budget_exhausted"
    domain.status_changed_at = now
    session.flush()
    if was != DomainStatus.ECOMMERCE or scan_run_id is None or not host.is_primary:
        return None
    if session.get(StoreProfile, host.id) is None:
        return None
    return _event(
        session,
        host.id,
        ChangeEventType.STORE_OFFLINE,
        domain.etld1,
        was,
        DomainStatus.UNREACHABLE,
        scan_run_id,
        now,
    )


def mark_online(
    session: Session, host: Host, domain: Domain, *, scan_run_id: uuid.UUID, now: datetime
) -> ChangeEvent | None:
    """Called when a scan classifies a previously unreachable domain as e-commerce again."""
    if not host.is_primary:
        return None
    last = session.execute(
        select(ChangeEvent.event_type)
        .where(
            ChangeEvent.host_id == host.id,
            ChangeEvent.entity_type == ENTITY_TYPE,
            ChangeEvent.event_type.in_(
                [ChangeEventType.STORE_OFFLINE, ChangeEventType.STORE_ONLINE]
            ),
        )
        .order_by(ChangeEvent.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if last != ChangeEventType.STORE_OFFLINE:
        return None  # only a store that went offline through us comes back online
    return _event(
        session,
        host.id,
        ChangeEventType.STORE_ONLINE,
        domain.etld1,
        DomainStatus.UNREACHABLE,
        DomainStatus.ECOMMERCE,
        scan_run_id,
        now,
    )
