"""C Report aggregates (FR-RP-01…03, LR-19).

All metrics are computed from the current state in PostgreSQL (`store_*`)
and the change events of the period; every published cell has at least
`min_cell` stores, smaller cells are merged into `other`. Monthly dynamics
use `mv_market_share_monthly` in ClickHouse when a client is given, otherwise
the month of `first_seen` of current providers (documented in the
methodology sheet).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from payintel.core.models.base import ChangeEventType, DomainStatus, ProviderRole
from payintel.core.models.domains import Domain, Host
from payintel.core.models.reference import PaymentMethod
from payintel.core.models.store import ChangeEvent, StorePaymentMethod, StoreProfile, StoreProvider
from payintel.entitlements.lineage_guard import c1_visible_clause
from payintel.reports.wilson import wilson

METRICS: tuple[str, ...] = (
    "psp_share",
    "method_share",
    "bnpl_share",
    "providers_per_store",
    "psp_flows",
    "monthly_dynamics",
)


@dataclass(frozen=True)
class ReportSpec:
    countries: tuple[str, ...]
    platforms: tuple[str, ...] = ()
    verticals: tuple[str, ...] = ()
    period_start: date | None = None
    period_end: date | None = None
    metrics: tuple[str, ...] = METRICS
    min_cell: int = 30

    def as_dict(self) -> dict[str, Any]:
        return {
            "countries": list(self.countries),
            "platforms": list(self.platforms),
            "verticals": list(self.verticals),
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "metrics": list(self.metrics),
            "min_cell": self.min_cell,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ReportSpec:
        return cls(
            countries=tuple(d.get("countries", [])),
            platforms=tuple(d.get("platforms", [])),
            verticals=tuple(d.get("verticals", [])),
            period_start=date.fromisoformat(d["period_start"]) if d.get("period_start") else None,
            period_end=date.fromisoformat(d["period_end"]) if d.get("period_end") else None,
            metrics=tuple(d.get("metrics", METRICS)),
            min_cell=int(d.get("min_cell", 30)),
        )


@dataclass(frozen=True)
class ShareRow:
    group: str  # country / platform cell label
    key: str  # provider or method id (or "other")
    stores: int
    total: int

    @property
    def share(self) -> float:
        return self.stores / self.total if self.total else 0.0

    @property
    def ci(self) -> tuple[float, float]:
        return wilson(self.stores, self.total)


@dataclass
class Report:
    spec: ReportSpec
    generated_at: datetime
    total_stores: int
    cells: dict[str, int] = field(default_factory=dict)  # (country|platform) → stores
    psp_share: list[ShareRow] = field(default_factory=list)
    method_share: list[ShareRow] = field(default_factory=list)
    bnpl_share: list[ShareRow] = field(default_factory=list)
    providers_per_store: dict[str, float] = field(default_factory=dict)
    psp_flows: list[tuple[str, str, int]] = field(default_factory=list)  # from, to, stores
    monthly: list[tuple[str, str, int]] = field(default_factory=list)  # month, provider, stores
    suppressed_cells: int = 0

    def min_published(self) -> int:
        values = [
            r.stores
            for r in self.psp_share + self.method_share + self.bnpl_share
            if r.key != "other"
        ]
        values += [n for _, _, n in self.psp_flows] + [n for _, _, n in self.monthly]
        return min(values) if values else 0


def _scope(spec: ReportSpec) -> Any:
    q = (
        select(StoreProfile)
        .join(Host, Host.id == StoreProfile.host_id)
        .join(Domain, Domain.id == Host.domain_id)
        .where(
            Host.is_primary.is_(True), Domain.status == DomainStatus.ECOMMERCE, c1_visible_clause()
        )
    )
    if spec.countries:
        q = q.where(StoreProfile.country.in_([c.upper() for c in spec.countries]))
    if spec.platforms:
        q = q.where(StoreProfile.platform_id.in_(list(spec.platforms)))
    if spec.verticals:
        q = q.where(StoreProfile.vertical_id.in_(list(spec.verticals)))
    return q


def _cell_label(p: StoreProfile) -> str:
    return f"{p.country or '??'}|{p.platform_id or 'unknown'}"


def _suppress(
    counter: dict[str, Counter[str]], totals: dict[str, int], min_cell: int
) -> tuple[list[ShareRow], int]:
    rows: list[ShareRow] = []
    suppressed = 0
    for cell, keys in sorted(counter.items()):
        total = totals[cell]
        if total < min_cell:
            suppressed += 1
            continue
        other = 0
        for key, n in sorted(keys.items()):
            if n >= min_cell:
                rows.append(ShareRow(cell, key, n, total))
            else:
                other += n
        if other:
            rows.append(ShareRow(cell, "other", other, total))
    return rows, suppressed


def build(session: Session, spec: ReportSpec, *, now: datetime, ch: Any = None) -> Report:
    profiles = list(session.execute(_scope(spec)).scalars())
    host_ids = [p.host_id for p in profiles]
    cell_of = {p.host_id: _cell_label(p) for p in profiles}
    totals: dict[str, int] = Counter(cell_of.values())
    report = Report(spec=spec, generated_at=now, total_stores=len(profiles), cells=dict(totals))
    if not host_ids:
        return report
    providers = list(
        session.execute(select(StoreProvider).where(StoreProvider.host_id.in_(host_ids))).scalars()
    )
    methods = list(
        session.execute(
            select(StorePaymentMethod).where(StorePaymentMethod.host_id.in_(host_ids))
        ).scalars()
    )
    mtypes = {m.id: m.type.value for m in session.execute(select(PaymentMethod)).scalars()}

    if "psp_share" in spec.metrics:
        c: dict[str, Counter[str]] = defaultdict(Counter)
        for sp in providers:
            if sp.role in (
                ProviderRole.GATEWAY,
                ProviderRole.ORCHESTRATOR,
                ProviderRole.LOCAL_METHOD_PROVIDER,
            ):
                c[cell_of[sp.host_id]][sp.provider_id] += 1
        report.psp_share, s = _suppress(c, totals, spec.min_cell)
        report.suppressed_cells += s
    if "method_share" in spec.metrics:
        c = defaultdict(Counter)
        for sm in methods:
            c[cell_of[sm.host_id]][sm.method_id] += 1
        report.method_share, s = _suppress(c, totals, spec.min_cell)
        report.suppressed_cells += s
    if "bnpl_share" in spec.metrics:
        c = defaultdict(Counter)
        bnpl_hosts: set[int] = set()
        for sm in methods:
            if mtypes.get(sm.method_id) == "bnpl":
                bnpl_hosts.add(sm.host_id)
        for sp in providers:
            if sp.role == ProviderRole.BNPL:
                bnpl_hosts.add(sp.host_id)
        for h in bnpl_hosts:
            c[cell_of[h]]["bnpl"] += 1
        report.bnpl_share, s = _suppress(c, totals, spec.min_cell)
        report.suppressed_cells += s
    if "providers_per_store" in spec.metrics:
        per_host: Counter[int] = Counter(sp.host_id for sp in providers)
        sums: dict[str, list[int]] = defaultdict(list)
        for h in host_ids:
            sums[cell_of[h]].append(per_host.get(h, 0))
        report.providers_per_store = {
            cell: round(sum(v) / len(v), 3)
            for cell, v in sorted(sums.items())
            if len(v) >= spec.min_cell
        }
    if "psp_flows" in spec.metrics:
        report.psp_flows = flows(session, host_ids, spec)
    if "monthly_dynamics" in spec.metrics:
        report.monthly = monthly(session, providers, spec, ch=ch)
    return report


def flows(session: Session, host_ids: list[int], spec: ReportSpec) -> list[tuple[str, str, int]]:
    """Stores that removed PSP A and added PSP B within the period (FR-RP-02 matrix)."""
    q = select(ChangeEvent).where(
        ChangeEvent.host_id.in_(host_ids),
        ChangeEvent.entity_type == "provider",
        ChangeEvent.suppressed.is_(False),
        ChangeEvent.event_type.in_(
            [ChangeEventType.PROVIDER_ADDED, ChangeEventType.PROVIDER_REMOVED]
        ),
    )
    if spec.period_start:
        q = q.where(
            ChangeEvent.detected_at
            >= datetime.combine(spec.period_start, datetime.min.time(), tzinfo=UTC)
        )
    if spec.period_end:
        q = q.where(
            ChangeEvent.detected_at
            <= datetime.combine(spec.period_end, datetime.max.time(), tzinfo=UTC)
        )
    removed: dict[int, set[str]] = defaultdict(set)
    added: dict[int, set[str]] = defaultdict(set)
    for e in session.execute(q).scalars():
        if e.entity_id is None:
            continue
        (removed if e.event_type == ChangeEventType.PROVIDER_REMOVED else added)[e.host_id].add(
            e.entity_id
        )
    pairs: Counter[tuple[str, str]] = Counter()
    for h, outs in removed.items():
        for a in outs:
            for b in added.get(h, set()):
                if a != b:
                    pairs[(a, b)] += 1
    return [(a, b, n) for (a, b), n in sorted(pairs.items()) if n >= spec.min_cell]


def monthly(
    session: Session, providers: list[StoreProvider], spec: ReportSpec, *, ch: Any
) -> list[tuple[str, str, int]]:
    rows: Counter[tuple[str, str]] = Counter()
    if ch is not None:
        try:
            result = ch.query(
                "SELECT toString(month), provider_id, uniqMerge(stores) "
                "FROM mv_market_share_monthly WHERE country IN %(countries)s "
                "GROUP BY month, provider_id ORDER BY month, provider_id",
                parameters={"countries": list(spec.countries) or [""]},
            )
            for month, provider, n in result.result_rows:
                rows[(str(month)[:7], str(provider))] = int(n)
        except Exception:
            rows = Counter()
    if not rows:
        for sp in providers:
            rows[(sp.first_seen.strftime("%Y-%m"), sp.provider_id)] += 1
        # cumulative: a provider seen first in month m is still present later
        months = sorted({m for m, _ in rows})
        cumulative: Counter[tuple[str, str]] = Counter()
        running: Counter[str] = Counter()
        for m in months:
            for (mm, p), n in rows.items():
                if mm == m:
                    running[p] += n
            for p, n in running.items():
                cumulative[(m, p)] = n
        rows = cumulative
    return [(m, p, n) for (m, p), n in sorted(rows.items()) if n >= spec.min_cell]


def count_scope(session: Session, spec: ReportSpec) -> int:
    return int(
        session.execute(select(func.count()).select_from(_scope(spec).subquery())).scalar_one()
    )


from datetime import UTC  # noqa: E402
