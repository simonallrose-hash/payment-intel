"""Scan plans (FR-SC-01, FR-SC-03, FR-SC-07, FR-DS-09, LR-22).

- `ensure_light_plans` creates/refreshes `light` plans for hosts of scannable
  domains; opted-out domains are never planned (FR-DS-09) and existing plans of
  opted-out domains are removed.
- `ensure_checkout_plans` creates `checkout` plans for e-commerce domains,
  except TLDs excluded from heavy scans (LR-22).
- `interval_for` gives the planned cycle (7/30 days, watchlist 7 days).
- `prioritize_manual` is the admin override (FR-SC-07), audited.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import ColumnElement, delete, exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from payintel.core import audit
from payintel.core.clock import SYSTEM_CLOCK, Clock
from payintel.core.models.alerts import WatchlistItem
from payintel.core.models.audit import OptoutRequest
from payintel.core.models.base import DomainStatus, ScanStatus, ScanType
from payintel.core.models.domains import Domain, Host
from payintel.core.models.scans import ScanPlan, ScanRun
from payintel.core.models.store import StoreProfile
from payintel.core.settings import ScanSettings
from payintel.scheduler.priority import MANUAL_PRIORITY, PriorityInputs, compute_priority

SCANNABLE = (DomainStatus.CANDIDATE, DomainStatus.ECOMMERCE, DomainStatus.NOT_ECOMMERCE)
CHECKOUT_SCANNABLE = (DomainStatus.ECOMMERCE,)


@dataclass
class PlanResult:
    created: int = 0
    updated: int = 0
    removed_optout: int = 0


def is_opted_out(session: Session, etld1: str) -> bool:
    """FR-DS-09: domain flag or a verified opt-out request (FR-OO-02)."""
    flagged = session.execute(
        select(Domain.id).where(Domain.etld1 == etld1, Domain.optout.is_(True))
    ).first()
    if flagged:
        return True
    verified = session.execute(
        select(OptoutRequest.id).where(
            OptoutRequest.domain == etld1, OptoutRequest.verified_at.is_not(None)
        )
    ).first()
    return verified is not None


def optout_clause() -> ColumnElement[bool]:
    return or_(
        Domain.optout.is_(True),
        exists().where(
            OptoutRequest.domain == Domain.etld1, OptoutRequest.verified_at.is_not(None)
        ),
    )


def interval_for(
    scan_type: ScanType, status: DomainStatus, *, on_watchlist: bool, s: ScanSettings
) -> timedelta:
    if scan_type == ScanType.LIGHT:
        days = (
            s.light_interval_days_ecommerce
            if status == DomainStatus.ECOMMERCE
            else s.light_interval_days_candidate
        )
    else:
        days = s.checkout_interval_days_watchlist if on_watchlist else s.checkout_interval_days
    return timedelta(days=days)


def heavy_scan_excluded(etld1: str, s: ScanSettings) -> bool:
    tld = etld1.rsplit(".", 1)[-1]
    excluded = {t.lower() for t in s.excluded_heavy_scan_tlds}
    return tld in excluded or tld.encode("idna").decode("ascii") in excluded


def _watchlisted(session: Session, etld1s: Iterable[str]) -> set[str]:
    return set(
        session.execute(
            select(WatchlistItem.domain).where(WatchlistItem.domain.in_(list(etld1s)))
        ).scalars()
    )


def _last_success(
    session: Session, host_ids: Iterable[int], scan_type: ScanType
) -> dict[int, datetime]:
    rows = session.execute(
        select(ScanRun.host_id, func.max(ScanRun.finished_at))
        .where(
            ScanRun.host_id.in_(list(host_ids)),
            ScanRun.scan_type == scan_type,
            ScanRun.status.not_in([ScanStatus.BLOCKED, ScanStatus.TIMEOUT, ScanStatus.ERROR]),
            ScanRun.finished_at.is_not(None),
        )
        .group_by(ScanRun.host_id)
    )
    return {hid: ts for hid, ts in rows}


def remove_optout_plans(session: Session) -> int:
    optout_domains = select(Domain.id).where(optout_clause())
    host_ids = select(Host.id).where(Host.domain_id.in_(optout_domains))
    result = session.execute(delete(ScanPlan).where(ScanPlan.host_id.in_(host_ids)))
    return int(result.rowcount)  # type: ignore[attr-defined]


def ensure_plans(
    session: Session,
    scan_type: ScanType,
    *,
    s: ScanSettings,
    clock: Clock = SYSTEM_CLOCK,
    limit: int = 10_000,
) -> PlanResult:
    """Create plans for hosts without one and refresh priorities of existing ones."""
    now = clock.now()
    result = PlanResult(removed_optout=remove_optout_plans(session))
    statuses = SCANNABLE if scan_type == ScanType.LIGHT else CHECKOUT_SCANNABLE
    stmt = (
        select(Host, Domain)
        .join(Domain, Domain.id == Host.domain_id)
        .where(Domain.status.in_(statuses), ~optout_clause())
        .order_by(Domain.traffic_rank.nulls_last(), Host.id)
        .limit(limit)
    )
    pairs = session.execute(stmt).all()
    if not pairs:
        return result
    watch = _watchlisted(session, {d.etld1 for _h, d in pairs})
    last_ok = _last_success(session, [h.id for h, _d in pairs], scan_type)
    profiles = {
        p.host_id: p
        for p in session.execute(
            select(StoreProfile).where(StoreProfile.host_id.in_([h.id for h, _d in pairs]))
        ).scalars()
    }
    rows = []
    for host, domain in pairs:
        if scan_type == ScanType.CHECKOUT and heavy_scan_excluded(domain.etld1, s):
            continue
        on_watch = domain.etld1 in watch
        profile = profiles.get(host.id)
        has_checkout = bool(profile and profile.checkout_status is not None)
        interval = interval_for(scan_type, domain.status, on_watchlist=on_watch, s=s)
        last = last_ok.get(host.id)
        days = (now - last).total_seconds() / 86400 if last else None
        priority = compute_priority(
            PriorityInputs(
                is_ecommerce=domain.status == DomainStatus.ECOMMERCE,
                traffic_rank=domain.traffic_rank,
                has_checkout=has_checkout,
                on_watchlist=on_watch,
                days_since_success=days,
                interval_days=interval.days,
            ),
            s,
        )
        rows.append(
            {
                "host_id": host.id,
                "scan_type": scan_type,
                "priority": priority,
                "next_scan_at": (last + interval) if last else now,
                "fail_count": 0,
                "updated_at": now,
            }
        )
    if not rows:
        return result
    ins = pg_insert(ScanPlan).values(rows)
    stmt2 = ins.on_conflict_do_update(
        constraint="uq_scan_plan_host_id_scan_type",
        set_={"priority": ins.excluded.priority, "updated_at": ins.excluded.updated_at},
        where=(ScanPlan.priority < MANUAL_PRIORITY) & ScanPlan.locked_until.is_(None),
    ).returning(ScanPlan.id, ScanPlan.fail_count, ScanPlan.updated_at)
    before = set(
        session.execute(
            select(ScanPlan.host_id).where(
                ScanPlan.scan_type == scan_type, ScanPlan.host_id.in_([r["host_id"] for r in rows])
            )
        ).scalars()
    )
    touched = session.execute(stmt2).all()
    result.created = len([r for r in rows if r["host_id"] not in before])
    result.updated = len(touched) - result.created
    return result


def prioritize_manual(
    session: Session,
    etld1s: Iterable[str],
    *,
    scan_type: ScanType,
    actor: str,
    s: ScanSettings,
    clock: Clock = SYSTEM_CLOCK,
) -> int:
    """FR-SC-07: put the domains' hosts at the front of the queue, audited."""
    now = clock.now()
    wanted = [e.lower() for e in etld1s]
    pairs = session.execute(
        select(Host, Domain)
        .join(Domain, Domain.id == Host.domain_id)
        .where(Domain.etld1.in_(wanted))
    ).all()
    count = 0
    for host, domain in pairs:
        if is_opted_out(session, domain.etld1):
            continue
        if scan_type == ScanType.CHECKOUT and heavy_scan_excluded(domain.etld1, s):
            continue
        plan = session.execute(
            select(ScanPlan).where(ScanPlan.host_id == host.id, ScanPlan.scan_type == scan_type)
        ).scalar_one_or_none()
        if plan is None:
            plan = ScanPlan(host_id=host.id, scan_type=scan_type, next_scan_at=now)
            session.add(plan)
        before = {
            "priority": plan.priority,
            "next_scan_at": plan.next_scan_at.isoformat() if plan.next_scan_at else None,
        }
        plan.priority = MANUAL_PRIORITY
        plan.next_scan_at = now
        plan.fail_count = 0
        plan.updated_at = now
        audit.record(
            session,
            actor=actor,
            action="scan_plan.prioritize",
            object_type="host",
            object_id=host.hostname,
            before=before,
            after={"priority": MANUAL_PRIORITY, "scan_type": scan_type.value},
            clock=clock,
        )
        count += 1
    session.flush()
    return count
