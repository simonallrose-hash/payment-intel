"""PostgreSQL task queue with leases (FR-SC-04, NFR-R-05).

`lease` takes due plans with `SELECT … FOR UPDATE SKIP LOCKED`, so concurrent
workers never get the same task; `locked_until` is the lease. A crashed worker
leaves the row locked until the lease expires, after which any worker may take
it again. Retries are idempotent because the scan-run id is derived from the
plan id and the lease timestamp (`run_id_for`): re-executing the same lease
writes the same `scan_run` row.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from payintel.core.clock import SYSTEM_CLOCK, Clock
from payintel.core.errors import ConfigurationError
from payintel.core.models.base import ScanType
from payintel.core.models.domains import Domain, Host
from payintel.core.models.scans import ScanPlan
from payintel.core.settings import ScanSettings
from payintel.history import availability
from payintel.scheduler.backoff import retry_delay
from payintel.scheduler.priority import MANUAL_PRIORITY

RUN_NAMESPACE = uuid.UUID("6f1d2a1e-5c3b-4e8a-9d2f-7b1c0a9e8d77")


def run_id_for(plan: ScanPlan) -> uuid.UUID:
    if plan.locked_until is None:
        raise ConfigurationError("run ids exist only for leased plans")
    return uuid.uuid5(RUN_NAMESPACE, f"{plan.id}:{plan.locked_until.isoformat()}")


def lease(
    session: Session,
    scan_type: ScanType,
    worker_id: str,
    *,
    limit: int,
    lease_seconds: int,
    clock: Clock = SYSTEM_CLOCK,
) -> list[ScanPlan]:
    now = clock.now()
    until = now + timedelta(seconds=lease_seconds)
    candidates = (
        select(ScanPlan.id)
        .where(
            ScanPlan.scan_type == scan_type,
            ScanPlan.next_scan_at <= now,
            or_(ScanPlan.locked_until.is_(None), ScanPlan.locked_until < now),
        )
        .order_by(ScanPlan.priority.desc(), ScanPlan.next_scan_at, ScanPlan.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    stmt = (
        update(ScanPlan)
        .where(ScanPlan.id.in_(candidates))
        .values(locked_until=until, locked_by=worker_id, updated_at=now)
        .returning(ScanPlan.id)
    )
    ids = [row[0] for row in session.execute(stmt)]
    if not ids:
        return []
    plans = list(
        session.execute(
            select(ScanPlan)
            .where(ScanPlan.id.in_(ids))
            .order_by(ScanPlan.priority.desc(), ScanPlan.id)
        ).scalars()
    )
    for p in plans:
        session.refresh(p)
    return plans


def heartbeat(
    session: Session, plan: ScanPlan, *, lease_seconds: int, clock: Clock = SYSTEM_CLOCK
) -> None:
    plan.locked_until = clock.now() + timedelta(seconds=lease_seconds)
    session.flush()


def complete(
    session: Session, plan: ScanPlan, *, next_scan_at: datetime, clock: Clock = SYSTEM_CLOCK
) -> None:
    """Successful run: unlock, reset failures, schedule the next cycle; drop manual boost."""
    plan.locked_until = None
    plan.locked_by = None
    plan.fail_count = 0
    plan.last_error = None
    plan.next_scan_at = next_scan_at
    if plan.priority >= MANUAL_PRIORITY:
        plan.priority = 0.0
    plan.updated_at = clock.now()
    session.flush()


def fail(
    session: Session,
    plan: ScanPlan,
    *,
    error: str,
    cycle: timedelta,
    s: ScanSettings,
    clock: Clock = SYSTEM_CLOCK,
    scan_run_id: uuid.UUID | None = None,
) -> bool:
    """Failed run: backoff 1/6/24/72 h; after the budget → host unreachable (FR-SC-05).

    Returns True when the host was marked unreachable. With `scan_run_id` an
    e-commerce store that becomes unreachable also gets a `store_offline` event.
    """
    now = clock.now()
    plan.locked_until = None
    plan.locked_by = None
    plan.fail_count += 1
    plan.last_error = error[:2000]
    plan.updated_at = now
    delay = retry_delay(plan.fail_count, s)
    if delay is not None:
        plan.next_scan_at = now + delay
        session.flush()
        return False
    plan.next_scan_at = now + cycle
    plan.fail_count = 0
    host = session.get(Host, plan.host_id)
    if host is not None and host.is_primary:
        domain = session.get(Domain, host.domain_id)
        if domain is not None:
            availability.mark_offline(session, host, domain, scan_run_id=scan_run_id, now=now)
    session.flush()
    return True


def release(session: Session, plan: ScanPlan, *, clock: Clock = SYSTEM_CLOCK) -> None:
    """Give a task back untouched (worker shutting down)."""
    plan.locked_until = None
    plan.locked_by = None
    plan.updated_at = clock.now()
    session.flush()
