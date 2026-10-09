"""Store views for clients: lookup, search with filters, sort and keyset cursor.

Everything here is restricted twice: by `entitlements.lineage_guard`
(what may exist for a client at all) and by `entitlements.segment` (what this
organisation bought). Evidence for `c1_full` comes from ClickHouse when a
client is available and silently degrades to an empty list when it is not
(NFR-R-06).
"""

from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Protocol

from sqlalchemy import Select, exists, func, literal, select, tuple_
from sqlalchemy.orm import Session

from payintel.api.schemas import c1_basic, c1_full
from payintel.api.schemas.common import (
    CheckoutInfo,
    CheckoutInfoFull,
    CountryRef,
    Evidence,
    MethodRef,
    MethodRefFull,
    PlatformRef,
    ProviderRef,
    ProviderRefFull,
    VerticalRef,
)
from payintel.core.errors import NotFoundError, ValidationError
from payintel.core.models.base import ConfidenceLevel, FieldProfile
from payintel.core.models.domains import Domain, Host
from payintel.core.models.reference import PaymentMethod, Provider
from payintel.core.models.store import (
    ChangeEvent,
    StoreCheckoutHost,
    StorePaymentMethod,
    StoreProfile,
    StoreProvider,
)
from payintel.entitlements.lineage_guard import c1_visible_clause
from payintel.entitlements.model import Grant
from payintel.entitlements.segment import (
    countries_in_segment,
    platforms_in_segment,
    require_in_segment,
    segment_clause,
)

_CONF_RANK: dict[str, int] = {"low": 1, "medium": 2, "high": 3}
_RANK_NULL = 2**31 - 1
SORTS: tuple[str, ...] = ("domain", "traffic_rank", "updated_at")


class EvidenceSource(Protocol):
    def evidence(self, host_id: int, provider_id: str, limit: int) -> list[Evidence]: ...


class NoEvidence:
    def evidence(self, host_id: int, provider_id: str, limit: int) -> list[Evidence]:
        return []


class ClickHouseEvidence:
    """Last observations of a provider on a store; empty when ClickHouse is down."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def evidence(self, host_id: int, provider_id: str, limit: int) -> list[Evidence]:
        try:
            result = self._client.query(
                "SELECT signal_type, signal_value, page_type FROM obs_provider "
                "WHERE host_id = %(h)s AND provider_id = %(p)s "
                "ORDER BY scan_ts DESC LIMIT %(n)s",
                parameters={"h": host_id, "p": provider_id, "n": limit},
            )
        except Exception:
            return []
        seen: set[tuple[str, str, str]] = set()
        out: list[Evidence] = []
        for signal_type, value, page_type in result.result_rows:
            key = (str(signal_type), str(value), str(page_type))
            if key in seen:
                continue
            seen.add(key)
            out.append(Evidence(signal_type=key[0], value=key[1], page_type=key[2]))
        return out


@dataclass(frozen=True)
class StoreFilters:
    """FR-API-04."""

    countries: list[str] = field(default_factory=list)
    platforms: list[str] = field(default_factory=list)
    providers: list[str] = field(default_factory=list)
    without_providers: list[str] = field(default_factory=list)
    provider_roles: list[str] = field(default_factory=list)
    methods: list[str] = field(default_factory=list)
    providers_min: int | None = None
    providers_max: int | None = None
    min_confidence: str | None = None
    first_seen_from: date | None = None
    first_seen_to: date | None = None
    last_seen_from: date | None = None
    last_seen_to: date | None = None
    changed_from: datetime | None = None
    changed_to: datetime | None = None
    domain_prefix: str | None = None


@dataclass(frozen=True)
class StoreRow:
    host_id: int
    domain: str
    profile: StoreProfile


def _visible_base() -> Select[tuple[StoreProfile, Domain]]:
    return (
        select(StoreProfile, Domain)
        .join(Host, Host.id == StoreProfile.host_id)
        .join(Domain, Domain.id == Host.domain_id)
        .where(Host.is_primary.is_(True), c1_visible_clause())
    )


def lookup(session: Session, grant: Grant, domain: str) -> StoreRow:
    """One store by eTLD+1; 404 when unknown or not a C1 product, 403 outside segment."""
    row = session.execute(_visible_base().where(Domain.etld1 == domain.lower())).first()
    if row is None:
        raise NotFoundError("store not found", domain=domain)
    profile, dom = row
    require_in_segment(grant, country=profile.country, platform_id=profile.platform_id)
    return StoreRow(profile.host_id, dom.etld1, profile)


def _provider_count_subq() -> Any:
    return (
        select(func.count(StoreProvider.id))
        .where(StoreProvider.host_id == StoreProfile.host_id, StoreProvider.suppressed.is_(False))
        .correlate(StoreProfile)
        .scalar_subquery()
    )


def _apply_filters(q: Select[Any], grant: Grant, f: StoreFilters) -> Select[Any]:
    countries = countries_in_segment(grant, f.countries)
    if countries:
        q = q.where(StoreProfile.country.in_(countries))
    platforms = platforms_in_segment(grant, f.platforms)
    if platforms:
        q = q.where(StoreProfile.platform_id.in_(platforms))
    if f.providers:
        for pid in f.providers:
            q = q.where(
                exists().where(
                    StoreProvider.host_id == StoreProfile.host_id,
                    StoreProvider.provider_id == pid,
                    StoreProvider.suppressed.is_(False),
                )
            )
    for pid in f.without_providers:
        q = q.where(
            ~exists().where(
                StoreProvider.host_id == StoreProfile.host_id,
                StoreProvider.provider_id == pid,
                StoreProvider.suppressed.is_(False),
            )
        )
    if f.provider_roles:
        q = q.where(
            exists().where(
                StoreProvider.host_id == StoreProfile.host_id,
                StoreProvider.role.in_(f.provider_roles),
                StoreProvider.suppressed.is_(False),
            )
        )
    for mid in f.methods:
        q = q.where(
            exists().where(
                StorePaymentMethod.host_id == StoreProfile.host_id,
                StorePaymentMethod.method_id == mid,
                StorePaymentMethod.suppressed.is_(False),
            )
        )
    if f.providers_min is not None:
        q = q.where(_provider_count_subq() >= f.providers_min)
    if f.providers_max is not None:
        q = q.where(_provider_count_subq() <= f.providers_max)
    if f.min_confidence:
        allowed = [c for c, r in _CONF_RANK.items() if r >= _CONF_RANK[f.min_confidence]]
        q = q.where(
            exists().where(
                StoreProvider.host_id == StoreProfile.host_id,
                StoreProvider.confidence.in_(allowed),
                StoreProvider.suppressed.is_(False),
            )
        )
    seen_parts: list[Any] = [StoreProvider.suppressed.is_(False)]
    if f.first_seen_from:
        seen_parts.append(StoreProvider.first_seen >= f.first_seen_from)
    if f.first_seen_to:
        seen_parts.append(StoreProvider.first_seen <= f.first_seen_to)
    if f.last_seen_from:
        seen_parts.append(StoreProvider.last_seen >= f.last_seen_from)
    if f.last_seen_to:
        seen_parts.append(StoreProvider.last_seen <= f.last_seen_to)
    if len(seen_parts) > 1:
        q = q.where(exists().where(StoreProvider.host_id == StoreProfile.host_id, *seen_parts))
    if f.changed_from or f.changed_to:
        parts: list[Any] = [
            ChangeEvent.host_id == StoreProfile.host_id,
            ChangeEvent.suppressed.is_(False),
        ]
        if f.changed_from:
            parts.append(ChangeEvent.detected_at >= f.changed_from)
        if f.changed_to:
            parts.append(ChangeEvent.detected_at <= f.changed_to)
        q = q.where(exists().where(*parts))
    if f.domain_prefix:
        q = q.where(Domain.etld1.like(f.domain_prefix.lower().replace("%", "") + "%"))
    return q


def encode_cursor(sort: str, value: Any, host_id: int) -> str:
    raw = json.dumps({"o": sort, "s": value, "i": host_id}, default=str).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str, sort: str) -> tuple[Any, int]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
        if data["o"] != sort:
            raise ValidationError("cursor does not match the sort order")
        return data["s"], int(data["i"])
    except (ValueError, KeyError, TypeError) as exc:
        raise ValidationError("invalid cursor") from exc


def _sort_key(sort: str) -> Any:
    if sort == "domain":
        return Domain.etld1
    if sort == "traffic_rank":
        return func.coalesce(StoreProfile.traffic_rank, literal(_RANK_NULL))
    return StoreProfile.updated_at


def search(
    session: Session,
    grant: Grant,
    filters: StoreFilters,
    *,
    sort: str = "domain",
    cursor: str | None = None,
    limit: int = 100,
) -> tuple[list[StoreRow], str | None]:
    """Keyset pagination (FR-API-05): `(sort key, host_id)` strictly after the cursor."""
    if sort not in SORTS:
        raise ValidationError("unknown sort", sort=sort)
    q = _apply_filters(_visible_base().where(segment_clause(grant)), grant, filters)
    key = _sort_key(sort)
    descending = sort == "updated_at"
    if cursor:
        value, host_id = decode_cursor(cursor, sort)
        if sort == "updated_at":
            value = datetime.fromisoformat(value)
        pair = tuple_(key, StoreProfile.host_id)
        q = q.where(pair < (value, host_id) if descending else pair > (value, host_id))
    order = (key.desc(), StoreProfile.host_id.desc()) if descending else (key, StoreProfile.host_id)
    rows = session.execute(q.order_by(*order).limit(limit + 1)).all()
    page = [StoreRow(p.host_id, d.etld1, p) for p, d in rows[:limit]]
    next_cursor: str | None = None
    if len(rows) > limit and page:
        last = page[-1]
        if sort == "domain":
            value = last.domain
        elif sort == "traffic_rank":
            value = (
                last.profile.traffic_rank if last.profile.traffic_rank is not None else _RANK_NULL
            )
        else:
            value = last.profile.updated_at.isoformat()
        next_cursor = encode_cursor(sort, value, last.host_id)
    return page, next_cursor


def _as_of(p: StoreProfile) -> datetime:
    candidates = [t for t in (p.last_light_scan_at, p.last_checkout_scan_at) if t is not None]
    return max(candidates) if candidates else p.updated_at


def _conf(level: ConfidenceLevel | None) -> Any:
    return level.value if level is not None else None


def _names(session: Session, table: Any, ids: set[str]) -> dict[str, Any]:
    if not ids:
        return {}
    rows = session.execute(select(table).where(table.id.in_(sorted(ids)))).scalars()
    return {r.id: r for r in rows}


def _conf_ok(level: ConfidenceLevel, min_confidence: str | None) -> bool:
    return min_confidence is None or _CONF_RANK[level.value] >= _CONF_RANK[min_confidence]


def build_store(
    session: Session,
    row: StoreRow,
    *,
    profile: FieldProfile,
    methodology_url: str,
    evidence: EvidenceSource | None = None,
    min_confidence: str | None = None,
    evidence_limit: int = 3,
) -> c1_basic.StoreBasic | c1_full.StoreFull:
    p = row.profile
    providers = [
        r
        for r in session.execute(
            select(StoreProvider)
            .where(StoreProvider.host_id == row.host_id, StoreProvider.suppressed.is_(False))
            .order_by(StoreProvider.provider_id)
        ).scalars()
        if _conf_ok(r.confidence, min_confidence)
    ]
    methods = [
        r
        for r in session.execute(
            select(StorePaymentMethod)
            .where(
                StorePaymentMethod.host_id == row.host_id,
                StorePaymentMethod.suppressed.is_(False),
            )
            .order_by(StorePaymentMethod.method_id)
        ).scalars()
        if _conf_ok(r.confidence, min_confidence)
    ]
    pnames = _names(session, Provider, {r.provider_id for r in providers})
    mref = _names(session, PaymentMethod, {r.method_id for r in methods})
    platform = (
        PlatformRef(id=p.platform_id, confidence=_conf(p.platform_confidence))
        if p.platform_id
        else None
    )
    country = (
        CountryRef(code=p.country, confidence=_conf(p.country_confidence)) if p.country else None
    )
    coverage = p.coverage.value if p.coverage else None
    status = p.checkout_status.value if p.checkout_status else None
    confidence = _conf(p.platform_confidence)
    if profile == FieldProfile.C1_BASIC:
        return c1_basic.StoreBasic(
            domain=row.domain,
            as_of=_as_of(p),
            confidence=confidence,
            coverage=coverage,
            platform=platform,
            country=country,
            checkout=CheckoutInfo(status=status, coverage=coverage),
            providers=[
                ProviderRef(
                    id=r.provider_id,
                    name=pnames[r.provider_id].name if r.provider_id in pnames else r.provider_id,
                    role=r.role.value,
                    confidence=r.confidence.value,
                    first_seen=r.first_seen,
                    last_seen=r.last_seen,
                )
                for r in providers
            ],
            payment_methods=[
                MethodRef(
                    id=r.method_id,
                    type=mref[r.method_id].type.value if r.method_id in mref else "other",
                    confidence=r.confidence.value,
                )
                for r in methods
            ],
            methodology_url=methodology_url,
        )
    ev = evidence or NoEvidence()
    psp_hosts = [
        h
        for h in session.execute(
            select(StoreCheckoutHost.third_party_etld1)
            .where(StoreCheckoutHost.host_id == row.host_id, StoreCheckoutHost.category == "psp")
            .order_by(StoreCheckoutHost.third_party_etld1)
        ).scalars()
    ]
    recent = session.execute(
        select(ChangeEvent)
        .where(ChangeEvent.host_id == row.host_id, ChangeEvent.suppressed.is_(False))
        .order_by(ChangeEvent.detected_at.desc(), ChangeEvent.id.desc())
        .limit(10)
    ).scalars()
    return c1_full.StoreFull(
        domain=row.domain,
        as_of=_as_of(p),
        confidence=confidence,
        coverage=coverage,
        platform=platform,
        country=country,
        currency=p.currency,
        vertical=VerticalRef(id=p.vertical_id, confidence=_conf(p.vertical_confidence))
        if p.vertical_id
        else None,
        checkout=CheckoutInfoFull(
            status=status,
            coverage=coverage,
            acquirer_hidden=p.acquirer_hidden,
            checkout_country=p.checkout_country,
        ),
        providers=[
            ProviderRefFull(
                id=r.provider_id,
                name=pnames[r.provider_id].name if r.provider_id in pnames else r.provider_id,
                role=r.role.value,
                confidence=r.confidence.value,
                first_seen=r.first_seen,
                last_seen=r.last_seen,
                active_on_checkout=r.active_on_checkout,
                evidence=ev.evidence(row.host_id, r.provider_id, evidence_limit),
            )
            for r in providers
        ],
        payment_methods=[
            MethodRefFull(
                id=r.method_id,
                type=mref[r.method_id].type.value if r.method_id in mref else "other",
                confidence=r.confidence.value,
                provider_id=r.provider_id,
                first_seen=r.first_seen,
                last_seen=r.last_seen,
            )
            for r in methods
        ],
        checkout_psp_hosts=psp_hosts,
        recent_changes=[
            c1_full.RecentChange(
                type=e.event_type.value, entity=e.entity_id, detected_at=e.detected_at
            )
            for e in recent
        ],
        traffic_rank=p.traffic_rank,
        last_light_scan_at=p.last_light_scan_at,
        last_checkout_scan_at=p.last_checkout_scan_at,
        methodology_url=methodology_url,
    )


def history(
    session: Session, row: StoreRow, *, profile: FieldProfile, limit: int, cursor: str | None
) -> tuple[list[c1_basic.ChangeBasic | c1_full.ChangeFull], str | None]:
    q = (
        select(ChangeEvent)
        .where(ChangeEvent.host_id == row.host_id, ChangeEvent.suppressed.is_(False))
        .order_by(ChangeEvent.id.desc())
    )
    if cursor:
        _, last_id = decode_cursor(cursor, "history")
        q = q.where(ChangeEvent.id < last_id)
    events = list(session.execute(q.limit(limit + 1)).scalars())
    page = events[:limit]
    nxt = encode_cursor("history", None, page[-1].id) if len(events) > limit and page else None
    return [_change(row.domain, e, profile) for e in page], nxt


def _change(domain: str, e: ChangeEvent, profile: FieldProfile) -> Any:
    if profile == FieldProfile.C1_BASIC:
        return c1_basic.ChangeBasic(
            domain=domain, type=e.event_type.value, entity=e.entity_id, detected_at=e.detected_at
        )
    return c1_full.ChangeFull(
        domain=domain,
        type=e.event_type.value,
        entity=e.entity_id,
        old_value=e.old_value,
        new_value=e.new_value,
        detected_at=e.detected_at,
    )


def changes(
    session: Session,
    grant: Grant,
    *,
    profile: FieldProfile,
    since: datetime | None,
    until: datetime | None,
    event_types: Sequence[str],
    countries: list[str],
    platforms: list[str],
    limit: int,
    cursor: str | None,
) -> tuple[list[Any], str | None]:
    """Event feed over the segment (FR-API-03 `/v1/changes`)."""
    q = (
        select(ChangeEvent, Domain.etld1)
        .join(StoreProfile, StoreProfile.host_id == ChangeEvent.host_id)
        .join(Host, Host.id == ChangeEvent.host_id)
        .join(Domain, Domain.id == Host.domain_id)
        .where(
            ChangeEvent.suppressed.is_(False), c1_visible_clause(events=True), segment_clause(grant)
        )
        .order_by(ChangeEvent.id.desc())
    )
    cs = countries_in_segment(grant, countries)
    if cs:
        q = q.where(StoreProfile.country.in_(cs))
    ps = platforms_in_segment(grant, platforms)
    if ps:
        q = q.where(StoreProfile.platform_id.in_(ps))
    if since:
        q = q.where(ChangeEvent.detected_at >= since)
    if until:
        q = q.where(ChangeEvent.detected_at <= until)
    if event_types:
        q = q.where(ChangeEvent.event_type.in_(list(event_types)))
    if cursor:
        _, last_id = decode_cursor(cursor, "changes")
        q = q.where(ChangeEvent.id < last_id)
    rows = session.execute(q.limit(limit + 1)).all()
    page = rows[:limit]
    nxt = encode_cursor("changes", None, page[-1][0].id) if len(rows) > limit and page else None
    return [_change(etld1, e, profile) for e, etld1 in page], nxt


def market_share(
    session: Session,
    grant: Grant,
    *,
    countries: list[str],
    platforms: list[str],
    role: str | None,
    min_cell: int,
) -> tuple[int, list[tuple[str | None, str | None, str, int]]]:
    """Stores per (country, platform, provider) in the segment.

    LR-19 / FR-RP-03: a (country, platform) cell with fewer than `min_cell`
    stores is withheld entirely; inside a published cell, providers present in
    fewer than `min_cell` stores are merged into `other` (distinct stores).
    """
    base = (
        select(StoreProfile.host_id)
        .join(Host, Host.id == StoreProfile.host_id)
        .join(Domain, Domain.id == Host.domain_id)
        .where(Host.is_primary.is_(True), c1_visible_clause(), segment_clause(grant))
    )
    cs = countries_in_segment(grant, countries)
    if cs:
        base = base.where(StoreProfile.country.in_(cs))
    ps = platforms_in_segment(grant, platforms)
    if ps:
        base = base.where(StoreProfile.platform_id.in_(ps))
    hosts = base.subquery()
    cell_sizes: dict[tuple[str | None, str | None], int] = {}
    for country, platform, n in session.execute(
        select(StoreProfile.country, StoreProfile.platform_id, func.count())
        .where(StoreProfile.host_id.in_(select(hosts.c.host_id)))
        .group_by(StoreProfile.country, StoreProfile.platform_id)
    ):
        cell_sizes[(country, platform)] = int(n)
    total = sum(cell_sizes.values())
    q = (
        select(
            StoreProfile.country,
            StoreProfile.platform_id,
            StoreProvider.provider_id,
            StoreProvider.host_id,
        )
        .join(StoreProfile, StoreProfile.host_id == StoreProvider.host_id)
        .where(
            StoreProvider.host_id.in_(select(hosts.c.host_id)),
            StoreProvider.suppressed.is_(False),
        )
        .order_by(StoreProfile.country, StoreProfile.platform_id, StoreProvider.provider_id)
    )
    if role:
        q = q.where(StoreProvider.role == role)
    per_provider: dict[tuple[str | None, str | None, str], set[int]] = {}
    for country, platform, provider, host_id in session.execute(q):
        per_provider.setdefault((country, platform, provider), set()).add(int(host_id))
    cells: list[tuple[str | None, str | None, str, int]] = []
    other: dict[tuple[str | None, str | None], set[int]] = {}
    for (country, platform, provider), host_ids in sorted(
        per_provider.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]), kv[0][2])
    ):
        if cell_sizes.get((country, platform), 0) < min_cell:
            continue  # withheld cell
        if len(host_ids) >= min_cell:
            cells.append((country, platform, provider, len(host_ids)))
        else:
            other.setdefault((country, platform), set()).update(host_ids)
    for (country, platform), host_ids in sorted(
        other.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]))
    ):
        cells.append((country, platform, "other", len(host_ids)))
    return total, cells
