"""Market-share snapshot: refresh in SQL, read per request (NFR-P-05, ADR-0033).

A live aggregation over ~1.5M `store_provider` rows takes 10–45 s on the
stand; the threshold is 3 s. `refresh` recomputes every cell once (one pass
per provider role plus one for "any role") inside a single transaction, so
readers see either the previous snapshot or the new one, never a mix. The
API falls back to the live query only while the table is empty (first
deployment), so correctness never depends on the cron job.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Select, delete, func, insert, select, text
from sqlalchemy.orm import Session

from payintel.core.models.base import ProviderRole
from payintel.core.models.domains import Domain, Host
from payintel.core.models.stats import MarketShareCell
from payintel.core.models.store import StoreProfile, StoreProvider
from payintel.entitlements.lineage_guard import c1_visible_clause

OTHER = "other"


@dataclass(frozen=True)
class RefreshResult:
    rows: int
    roles: int
    computed_at: datetime


@dataclass(frozen=True)
class Snapshot:
    computed_at: datetime
    min_cell: int
    totals: dict[tuple[str | None, str | None], int]
    providers: list[tuple[str | None, str | None, str, int]]


def _visible_hosts() -> Select[tuple[int]]:
    return (
        select(StoreProfile.host_id)
        .join(Host, Host.id == StoreProfile.host_id)
        .join(Domain, Domain.id == Host.domain_id)
        .where(Host.is_primary.is_(True), c1_visible_clause())
    )


def _cell_rows(
    session: Session, *, role: str | None, min_cell: int, now: datetime
) -> list[dict[str, object]]:
    hosts = _visible_hosts().subquery("hosts")
    member = (
        select(
            StoreProfile.country.label("country"),
            StoreProfile.platform_id.label("platform_id"),
            StoreProvider.provider_id.label("provider_id"),
            StoreProvider.host_id.label("host_id"),
        )
        .join(StoreProfile, StoreProfile.host_id == StoreProvider.host_id)
        .where(StoreProvider.host_id.in_(select(hosts.c.host_id)))
        .where(StoreProvider.suppressed.is_(False))
    )
    if role is not None:
        member = member.where(StoreProvider.role == role)
    m = member.cte("member")
    counts = (
        select(
            m.c.country,
            m.c.platform_id,
            m.c.provider_id,
            func.count(func.distinct(m.c.host_id)).label("n"),
        )
        .group_by(m.c.country, m.c.platform_id, m.c.provider_id)
        .cte("counts")
    )
    rows: list[dict[str, object]] = []
    for country, platform, provider, n in session.execute(
        select(counts.c.country, counts.c.platform_id, counts.c.provider_id, counts.c.n)
    ):
        rows.append(
            {
                "role": role,
                "country": country,
                "platform_id": platform,
                "provider_id": provider,
                "stores": int(n),
                "min_cell": min_cell,
                "computed_at": now,
            }
        )
    merged = (
        select(m.c.country, m.c.platform_id, func.count(func.distinct(m.c.host_id)))
        .join(
            counts,
            (counts.c.country.is_not_distinct_from(m.c.country))
            & (counts.c.platform_id.is_not_distinct_from(m.c.platform_id))
            & (counts.c.provider_id == m.c.provider_id),
        )
        .where(counts.c.n < min_cell)
        .group_by(m.c.country, m.c.platform_id)
    )
    for country, platform, n in session.execute(merged):
        rows.append(
            {
                "role": role,
                "country": country,
                "platform_id": platform,
                "provider_id": OTHER,
                "stores": int(n),
                "min_cell": min_cell,
                "computed_at": now,
            }
        )
    return rows


REFRESH_STATEMENT_TIMEOUT = "30min"
REFRESH_WORK_MEM = "256MB"


def refresh(session: Session, *, min_cell: int, now: datetime) -> RefreshResult:
    """Rebuild every cell. Caller commits; readers switch atomically with the commit.

    A batch job: the API's 30 s statement timeout does not apply, and the
    sorts for `count(DISTINCT host_id)` over 1.5M rows get real memory
    (`SET LOCAL`, this transaction only).
    """
    session.execute(text(f"SET LOCAL statement_timeout = '{REFRESH_STATEMENT_TIMEOUT}'"))
    session.execute(text(f"SET LOCAL work_mem = '{REFRESH_WORK_MEM}'"))
    totals = session.execute(
        select(StoreProfile.country, StoreProfile.platform_id, func.count())
        .where(StoreProfile.host_id.in_(_visible_hosts()))
        .group_by(StoreProfile.country, StoreProfile.platform_id)
    ).all()
    rows: list[dict[str, object]] = [
        {
            "role": None,
            "country": country,
            "platform_id": platform,
            "provider_id": None,
            "stores": int(n),
            "min_cell": min_cell,
            "computed_at": now,
        }
        for country, platform, n in totals
    ]
    roles: list[str | None] = [None, *[r.value for r in ProviderRole]]
    for role in roles:
        rows.extend(_cell_rows(session, role=role, min_cell=min_cell, now=now))
    session.execute(delete(MarketShareCell))
    if rows:
        session.execute(insert(MarketShareCell), rows)
    session.flush()
    return RefreshResult(rows=len(rows), roles=len(roles), computed_at=now)


def load(
    session: Session,
    *,
    role: str | None,
    countries: list[str],
    platforms: list[str],
    segment_countries: list[str] | None,
    segment_platforms: list[str] | None,
) -> Snapshot | None:
    """Cells of the latest snapshot inside the segment and the request filters.

    `segment_*` None = unrestricted. Returns None when no snapshot exists yet.
    """
    computed_at = session.execute(select(func.max(MarketShareCell.computed_at))).scalar()
    if computed_at is None:
        return None

    def cell_filter(q: Select[tuple[MarketShareCell]]) -> Select[tuple[MarketShareCell]]:
        if segment_countries is not None:
            q = q.where(MarketShareCell.country.in_(segment_countries))
        if segment_platforms is not None:
            q = q.where(MarketShareCell.platform_id.in_(segment_platforms))
        if countries:
            q = q.where(MarketShareCell.country.in_(countries))
        if platforms:
            q = q.where(MarketShareCell.platform_id.in_(platforms))
        return q.where(MarketShareCell.computed_at == computed_at)

    totals_q = cell_filter(
        select(MarketShareCell).where(
            MarketShareCell.role.is_(None), MarketShareCell.provider_id.is_(None)
        )
    )
    totals = {(c.country, c.platform_id): c.stores for c in session.execute(totals_q).scalars()}
    role_clause = MarketShareCell.role == role if role else MarketShareCell.role.is_(None)
    providers_q = cell_filter(
        select(MarketShareCell)
        .where(role_clause, MarketShareCell.provider_id.is_not(None))
        .order_by(MarketShareCell.country, MarketShareCell.platform_id, MarketShareCell.provider_id)
    )
    min_cell = 0
    providers: list[tuple[str | None, str | None, str, int]] = []
    for c in session.execute(providers_q).scalars():
        min_cell = c.min_cell
        if c.provider_id is not None:
            providers.append((c.country, c.platform_id, c.provider_id, c.stores))
    return Snapshot(computed_at=computed_at, min_cell=min_cell, totals=totals, providers=providers)
