"""Row builders for the three export types (FR-EX-02) within a grant's segment."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.api import read
from payintel.core.models.reference import PaymentMethod
from payintel.core.models.store import StoreCheckoutHost, StorePaymentMethod, StoreProvider
from payintel.entitlements.model import Grant
from payintel.reports.wilson import wilson


def _chunks(items: list[int], size: int) -> list[list[int]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _method_types(session: Session) -> dict[str, str]:
    return {m.id: m.type.value for m in session.execute(select(PaymentMethod)).scalars()}


def snapshot_rows(
    session: Session,
    grant: Grant,
    *,
    countries: list[str],
    platforms: list[str],
    page_size: int = 5_000,
) -> list[dict[str, Any]]:
    filters = read.StoreFilters(countries=countries, platforms=platforms)
    types = _method_types(session)
    out: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        page, cursor = read.search(
            session, grant, filters, sort="domain", cursor=cursor, limit=page_size
        )
        if not page:
            break
        ids = [r.host_id for r in page]
        providers: dict[int, list[StoreProvider]] = defaultdict(list)
        methods: dict[int, list[StorePaymentMethod]] = defaultdict(list)
        psp_hosts: dict[int, list[str]] = defaultdict(list)
        for chunk in _chunks(ids, 1_000):
            for sp in session.execute(
                select(StoreProvider)
                .where(StoreProvider.host_id.in_(chunk))
                .order_by(StoreProvider.provider_id)
            ).scalars():
                providers[sp.host_id].append(sp)
            for sm in session.execute(
                select(StorePaymentMethod)
                .where(StorePaymentMethod.host_id.in_(chunk))
                .order_by(StorePaymentMethod.method_id)
            ).scalars():
                methods[sm.host_id].append(sm)
            for host_id, etld1 in session.execute(
                select(StoreCheckoutHost.host_id, StoreCheckoutHost.third_party_etld1)
                .where(StoreCheckoutHost.host_id.in_(chunk), StoreCheckoutHost.category == "psp")
                .order_by(StoreCheckoutHost.third_party_etld1)
            ):
                psp_hosts[host_id].append(etld1)
        for r in page:
            p = r.profile
            ps = providers.get(r.host_id, [])
            ms = methods.get(r.host_id, [])
            seen_first = [sp.first_seen for sp in ps]
            seen_last = [sp.last_seen for sp in ps]
            scans = [t for t in (p.last_light_scan_at, p.last_checkout_scan_at) if t]
            out.append(
                {
                    "domain": r.domain,
                    "as_of": max(scans) if scans else p.updated_at,
                    "platform_id": p.platform_id,
                    "platform_confidence": p.platform_confidence.value
                    if p.platform_confidence
                    else None,
                    "country": p.country,
                    "country_confidence": p.country_confidence.value
                    if p.country_confidence
                    else None,
                    "currency": p.currency,
                    "vertical_id": p.vertical_id,
                    "checkout_status": p.checkout_status.value if p.checkout_status else None,
                    "coverage": p.coverage.value if p.coverage else None,
                    "acquirer_hidden": p.acquirer_hidden,
                    "provider_ids": [sp.provider_id for sp in ps],
                    "providers": [
                        f"{sp.provider_id}:{sp.role.value}:{sp.confidence.value}" for sp in ps
                    ],
                    "providers_active_on_checkout": [
                        sp.provider_id for sp in ps if sp.active_on_checkout
                    ],
                    "method_ids": [sm.method_id for sm in ms],
                    "methods": [
                        f"{sm.method_id}:{types.get(sm.method_id, 'other')}:{sm.confidence.value}"
                        for sm in ms
                    ],
                    "checkout_psp_hosts": psp_hosts.get(r.host_id, []),
                    "traffic_rank": p.traffic_rank,
                    "first_seen": min(seen_first) if seen_first else None,
                    "last_seen": max(seen_last) if seen_last else None,
                    "last_checkout_scan_at": p.last_checkout_scan_at,
                }
            )
        if cursor is None:
            break
    return out


def increment_rows(
    session: Session,
    grant: Grant,
    *,
    since: date | None,
    until: datetime | None,
    countries: list[str],
    platforms: list[str],
    page_size: int = 5_000,
) -> list[dict[str, Any]]:
    from payintel.core.models.base import FieldProfile

    out: list[dict[str, Any]] = []
    cursor: str | None = None
    since_dt = datetime.combine(since, datetime.min.time(), tzinfo=UTC_TZ) if since else None
    while True:
        page, cursor = read.changes(
            session,
            grant,
            profile=FieldProfile.C1_FULL,
            since=since_dt,
            until=until,
            event_types=(),
            countries=countries,
            platforms=platforms,
            limit=page_size,
            cursor=cursor,
        )
        for c in page:
            out.append(
                {
                    "domain": c.domain,
                    "event_type": c.type,
                    "entity": c.entity,
                    "old_value": c.old_value,
                    "new_value": c.new_value,
                    "detected_at": c.detected_at,
                }
            )
        if cursor is None:
            break
    return out


def aggregate_rows(
    session: Session,
    grant: Grant,
    *,
    countries: list[str],
    platforms: list[str],
    min_cell: int,
) -> list[dict[str, Any]]:
    _total, cells = read.market_share(
        session, grant, countries=countries, platforms=platforms, role=None, min_cell=min_cell
    )
    # Cell totals: unique stores per (country, platform) in the segment.
    totals = cell_totals(session, grant, countries=countries, platforms=platforms)
    out: list[dict[str, Any]] = []
    for country, platform, provider, n in cells:
        t = totals.get((country, platform), 0)
        lo, hi = wilson(n, t)
        out.append(
            {
                "country": country,
                "platform_id": platform,
                "provider_id": provider,
                "stores": n,
                "share": (n / t) if t else 0.0,
                "ci_low": lo,
                "ci_high": hi,
            }
        )
    return out


def cell_totals(
    session: Session, grant: Grant, *, countries: list[str], platforms: list[str]
) -> dict[tuple[str | None, str | None], int]:
    from sqlalchemy import func

    from payintel.core.models.domains import Domain, Host
    from payintel.core.models.store import StoreProfile
    from payintel.entitlements.lineage_guard import c1_visible_clause
    from payintel.entitlements.segment import (
        countries_in_segment,
        platforms_in_segment,
        segment_clause,
    )

    q = (
        select(StoreProfile.country, StoreProfile.platform_id, func.count(StoreProfile.host_id))
        .join(Host, Host.id == StoreProfile.host_id)
        .join(Domain, Domain.id == Host.domain_id)
        .where(Host.is_primary.is_(True), c1_visible_clause(), segment_clause(grant))
        .group_by(StoreProfile.country, StoreProfile.platform_id)
    )
    cs = countries_in_segment(grant, countries)
    if cs:
        q = q.where(StoreProfile.country.in_(cs))
    ps = platforms_in_segment(grant, platforms)
    if ps:
        q = q.where(StoreProfile.platform_id.in_(ps))
    return {(c, p): int(n) for c, p, n in session.execute(q)}


from datetime import UTC as UTC_TZ  # noqa: E402
