"""Quality dashboard (FR-QA-03): the numbers; stage 3 renders them in the admin UI.

- walk outcomes per platform over the window (`obs_scan`, checkout scans):
  share that reached the checkout, share that reached the payment step,
  share `blocked`;
- confidence distribution of the current state (`store_provider`,
  `store_payment_method` in PostgreSQL);
- freshness: how old the last light / checkout scan of the profiles is
  (median, share older than the planned cycle, never scanned);
- change events per day with spike detection: a day is a spike when its
  count is at least `spike_multiplier` × the mean of the 7 preceding days
  and at least `spike_min_events` (FR-QA-04 uses the same rule per provider).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median
from typing import Any

from clickhouse_connect.driver.client import Client
from sqlalchemy import func, literal_column, select
from sqlalchemy.orm import Session

from payintel.core.errors import NotFoundError, ValidationError
from payintel.core.models.alerts import WatchlistItem
from payintel.core.models.base import DomainStatus, ScanStatus
from payintel.core.models.domains import Domain, Host
from payintel.core.models.quality import QualityAlert
from payintel.core.models.scans import ScanRun
from payintel.core.models.store import ChangeEvent, StorePaymentMethod, StoreProfile, StoreProvider
from payintel.core.settings import QualitySettings

REACHED_CHECKOUT = frozenset(
    {ScanStatus.REACHED_CHECKOUT.value, ScanStatus.REACHED_PAYMENT_STEP.value}
)
SPIKE_MULTIPLIER = 3.0
SPIKE_MIN_EVENTS = 10
MIN_WALKS_FOR_REACH = 20  # NFR-Q-06: a share on fewer walks is noise
KIND_REACH = "checkout_reach_low"
KIND_FRESH_LIGHT = "freshness_light"
KIND_FRESH_CHECKOUT = "freshness_checkout"
KIND_FRESH_WATCHLIST = "freshness_watchlist"
KIND_FALSE_EVENTS = "false_change_events"
REVIEW_ACCEPTED = "accepted"
REVIEW_REJECTED = "rejected"


@dataclass(frozen=True)
class PlatformOutcome:
    platform_id: str
    walks: int
    to_checkout: float
    to_payment: float
    blocked: float


@dataclass(frozen=True)
class Freshness:
    """NFR-Q-07/08 populations: light scan over e-commerce profiles, heavy scan over
    profiles with a checkout, watchlist over hosts on any client watchlist."""

    profiles: int
    light_median_days: float | None
    light_stale_share: float  # older than `light_cycle_days`
    light_never: int
    checkout_median_days: float | None
    checkout_stale_share: float  # older than `checkout_cycle_days`
    checkout_never: int
    watchlist_profiles: int = 0
    watchlist_stale_share: float = 0.0  # older than `watchlist_cycle_days`


@dataclass(frozen=True)
class EventReview:
    """NFR-Q-09: verdicts of the manual review of sampled change events."""

    reviewed: int
    rejected: int

    @property
    def rejected_share(self) -> float | None:
        return round(self.rejected / self.reviewed, 4) if self.reviewed else None


@dataclass(frozen=True)
class Breach:
    """A quality threshold of ТЗ 7.2 that the current window violates."""

    code: str
    kind: str
    subject: str
    value: float
    threshold: float
    message: str


@dataclass(frozen=True)
class DayEvents:
    day: datetime
    count: int
    spike: bool


@dataclass
class QualityDashboard:
    since: datetime
    until: datetime
    outcomes: list[PlatformOutcome]
    overall: PlatformOutcome | None
    provider_confidence: dict[str, int]
    method_confidence: dict[str, int]
    freshness: Freshness
    events_per_day: list[DayEvents]
    spikes: list[DayEvents] = field(default_factory=list)
    event_review: EventReview = field(default_factory=lambda: EventReview(0, 0))

    def as_dict(self) -> dict[str, Any]:
        return {
            "since": self.since.isoformat(),
            "until": self.until.isoformat(),
            "outcomes": [o.__dict__ for o in self.outcomes],
            "overall": self.overall.__dict__ if self.overall else None,
            "provider_confidence": self.provider_confidence,
            "method_confidence": self.method_confidence,
            "freshness": self.freshness.__dict__,
            "event_review": {
                "reviewed": self.event_review.reviewed,
                "rejected": self.event_review.rejected,
                "rejected_share": self.event_review.rejected_share,
            },
            "events_per_day": [
                {"day": d.day.date().isoformat(), "count": d.count, "spike": d.spike}
                for d in self.events_per_day
            ],
            "spikes": [d.day.date().isoformat() for d in self.spikes],
        }


def walk_outcomes(
    ch: Client, *, since: datetime, until: datetime
) -> tuple[list[PlatformOutcome], PlatformOutcome | None]:
    rows = ch.query(
        "SELECT platform_id, status, count() AS n FROM obs_scan "
        "WHERE scan_type = 'checkout' AND scan_ts >= %(since)s AND scan_ts < %(until)s "
        "GROUP BY platform_id, status",
        parameters={"since": since, "until": until},
    ).result_rows
    per: dict[str, dict[str, int]] = {}
    for platform, status, n in rows:
        per.setdefault(str(platform), {})[str(status)] = int(n)

    def outcome(platform: str, counts: dict[str, int]) -> PlatformOutcome:
        walks = sum(counts.values())
        reached_checkout = sum(n for st, n in counts.items() if st in REACHED_CHECKOUT)
        reached_payment = counts.get(ScanStatus.REACHED_PAYMENT_STEP.value, 0)
        blocked = counts.get(ScanStatus.BLOCKED.value, 0)
        return PlatformOutcome(
            platform,
            walks,
            round(reached_checkout / walks, 4),
            round(reached_payment / walks, 4),
            round(blocked / walks, 4),
        )

    outcomes = sorted((outcome(p or "(none)", c) for p, c in per.items()), key=lambda o: -o.walks)
    total: dict[str, int] = {}
    for counts in per.values():
        for st, n in counts.items():
            total[st] = total.get(st, 0) + n
    return outcomes, outcome("all", total) if total else None


def confidence_distribution(session: Session) -> tuple[dict[str, int], dict[str, int]]:
    prov = session.execute(
        select(StoreProvider.confidence, func.count()).group_by(StoreProvider.confidence)
    ).all()
    meth = session.execute(
        select(StorePaymentMethod.confidence, func.count()).group_by(StorePaymentMethod.confidence)
    ).all()
    return (
        {str(c.value): int(n) for c, n in prov},
        {str(c.value): int(n) for c, n in meth},
    )


def _stale_share(ages: list[float], cycle_days: int) -> float:
    return round(sum(a > cycle_days for a in ages) / len(ages), 4) if ages else 0.0


def freshness(
    session: Session,
    *,
    now: datetime,
    light_cycle_days: int,
    checkout_cycle_days: int,
    watchlist_cycle_days: int = 8,
) -> Freshness:
    """Light freshness over profiles of `ecommerce` domains (NFR-Q-07); heavy freshness over
    profiles whose checkout was ever reached (NFR-Q-08); watchlist freshness over hosts on a
    client watchlist (NFR-Q-08, 8 days)."""
    rows = session.execute(
        select(
            StoreProfile.last_light_scan_at,
            StoreProfile.last_checkout_scan_at,
            StoreProfile.checkout_status,
            Domain.status,
            Host.hostname.in_(select(WatchlistItem.domain)),
        )
        .join(Host, Host.id == StoreProfile.host_id)
        .join(Domain, Domain.id == Host.domain_id)
    ).all()

    def age(t: datetime) -> float:
        return (now - t).total_seconds() / 86400

    ecommerce = [r for r in rows if r[3] == DomainStatus.ECOMMERCE]
    light_ages = [age(r[0]) for r in ecommerce if r[0] is not None]
    with_checkout = [r for r in rows if r[2] is not None]
    checkout_ages = [age(r[1]) for r in with_checkout if r[1] is not None]
    watch = [r for r in rows if r[4]]
    watch_ages = [age(r[1]) for r in watch if r[1] is not None]
    return Freshness(
        profiles=len(rows),
        light_median_days=round(median(light_ages), 2) if light_ages else None,
        light_stale_share=_stale_share(light_ages, light_cycle_days),
        light_never=len(ecommerce) - len(light_ages),
        checkout_median_days=round(median(checkout_ages), 2) if checkout_ages else None,
        checkout_stale_share=_stale_share(checkout_ages, checkout_cycle_days),
        checkout_never=len(with_checkout) - len(checkout_ages),
        watchlist_profiles=len(watch),
        watchlist_stale_share=_stale_share(watch_ages, watchlist_cycle_days)
        if watch_ages or not watch
        else 1.0,
    )


def event_review(session: Session) -> EventReview:
    """Counts of reviewed and rejected change events (NFR-Q-09)."""
    reviewed = session.scalar(
        select(func.count())
        .select_from(ChangeEvent)
        .where(ChangeEvent.review_decision.is_not(None))
    )
    rejected = session.scalar(
        select(func.count())
        .select_from(ChangeEvent)
        .where(ChangeEvent.review_decision == REVIEW_REJECTED)
    )
    return EventReview(int(reviewed or 0), int(rejected or 0))


def events_per_day(
    session: Session,
    *,
    now: datetime,
    days: int,
    multiplier: float = SPIKE_MULTIPLIER,
    min_events: int = SPIKE_MIN_EVENTS,
) -> list[DayEvents]:
    """Daily change-event counts for the last `days` days (plus the 7 before, for the baseline)."""
    start = (now - timedelta(days=days + 7)).replace(hour=0, minute=0, second=0, microsecond=0)
    day_of = func.date_trunc(literal_column("'day'"), ChangeEvent.detected_at)
    rows = session.execute(
        select(day_of, func.count())
        .where(ChangeEvent.detected_at >= start, ChangeEvent.detected_at < now)
        .group_by(day_of)
    ).all()
    counts = {d.date(): int(n) for d, n in rows}
    out: list[DayEvents] = []
    day = start
    while day < now:
        d = day.date()
        prev = [counts.get((day - timedelta(days=k)).date(), 0) for k in range(1, 8)]
        mean = sum(prev) / 7
        count = counts.get(d, 0)
        spike = count >= min_events and count >= multiplier * max(mean, 1.0)
        out.append(DayEvents(day, count, spike))
        day += timedelta(days=1)
    return out[-days:]


def dashboard(
    session: Session,
    ch: Client,
    *,
    now: datetime,
    days: int = 7,
    light_cycle_days: int = 7,
    checkout_cycle_days: int = 35,
    watchlist_cycle_days: int = 8,
) -> QualityDashboard:
    since, until = now - timedelta(days=days), now
    outcomes, overall = walk_outcomes(ch, since=since, until=until)
    prov, meth = confidence_distribution(session)
    per_day = events_per_day(session, now=now, days=days)
    return QualityDashboard(
        since=since,
        until=until,
        outcomes=outcomes,
        overall=overall,
        provider_confidence=prov,
        method_confidence=meth,
        freshness=freshness(
            session,
            now=now,
            light_cycle_days=light_cycle_days,
            checkout_cycle_days=checkout_cycle_days,
            watchlist_cycle_days=watchlist_cycle_days,
        ),
        events_per_day=per_day,
        spikes=[d for d in per_day if d.spike],
        event_review=event_review(session),
    )


def breaches(d: QualityDashboard, s: QualitySettings) -> list[Breach]:
    """Compare the dashboard with the ТЗ 7.2 thresholds (NFR-Q-06…09)."""
    out: list[Breach] = []
    by = {o.platform_id: o for o in d.outcomes}
    for platform in s.target_platforms:
        o = by.get(platform)
        if o is None or o.walks < MIN_WALKS_FOR_REACH:
            continue
        if o.to_checkout < s.min_checkout_reach_share:
            out.append(
                Breach(
                    "NFR-Q-06",
                    KIND_REACH,
                    platform,
                    o.to_checkout,
                    s.min_checkout_reach_share,
                    f"{platform}: {o.to_checkout * 100:.1f}% of {o.walks} walks reached the "
                    f"checkout (target ≥ {s.min_checkout_reach_share * 100:.0f}%)",
                )
            )
    f = d.freshness
    if f.light_stale_share > s.max_light_stale_share:
        out.append(
            Breach(
                "NFR-Q-07",
                KIND_FRESH_LIGHT,
                "ecommerce",
                f.light_stale_share,
                s.max_light_stale_share,
                f"{f.light_stale_share * 100:.1f}% of e-commerce profiles have a light scan "
                f"older than the cycle (target ≤ {s.max_light_stale_share * 100:.0f}%)",
            )
        )
    if f.checkout_stale_share > s.max_checkout_stale_share:
        out.append(
            Breach(
                "NFR-Q-08",
                KIND_FRESH_CHECKOUT,
                "checkout",
                f.checkout_stale_share,
                s.max_checkout_stale_share,
                f"{f.checkout_stale_share * 100:.1f}% of profiles with a checkout have a heavy "
                f"scan older than {s.checkout_freshness_days} days "
                f"(target ≤ {s.max_checkout_stale_share * 100:.0f}%)",
            )
        )
    if f.watchlist_profiles and f.watchlist_stale_share > s.max_watchlist_stale_share:
        out.append(
            Breach(
                "NFR-Q-08",
                KIND_FRESH_WATCHLIST,
                "watchlist",
                f.watchlist_stale_share,
                s.max_watchlist_stale_share,
                f"{f.watchlist_stale_share * 100:.1f}% of watchlist hosts have a heavy scan "
                f"older than {s.watchlist_freshness_days} days "
                f"(target ≤ {s.max_watchlist_stale_share * 100:.0f}%)",
            )
        )
    share = d.event_review.rejected_share
    if share is not None and share > s.max_false_event_share:
        out.append(
            Breach(
                "NFR-Q-09",
                KIND_FALSE_EVENTS,
                "change_events",
                share,
                s.max_false_event_share,
                f"{d.event_review.rejected} of {d.event_review.reviewed} reviewed change "
                f"events were rejected ({share * 100:.1f}%, target ≤ "
                f"{s.max_false_event_share * 100:.0f}%)",
            )
        )
    return out


def record_breaches(
    session: Session, found: Sequence[Breach], *, now: datetime, window_days: int
) -> list[QualityAlert]:
    """Write one open `quality_alert` per breach (kind, subject); returns the new rows."""
    open_alerts = {
        (str(k), str(sub))
        for k, sub in session.execute(
            select(QualityAlert.kind, QualityAlert.subject).where(
                QualityAlert.acknowledged_at.is_(None)
            )
        ).all()
    }
    created: list[QualityAlert] = []
    for b in found:
        if (b.kind, b.subject) in open_alerts:
            continue
        alert = QualityAlert(
            kind=b.kind,
            subject=b.subject,
            value=round(b.value, 4),
            baseline=None,
            threshold=b.threshold,
            window_days=window_days,
            message=f"{b.code}: {b.message}",
            detected_at=now,
        )
        session.add(alert)
        created.append(alert)
        open_alerts.add((b.kind, b.subject))
    session.flush()
    return created


def review_event(
    session: Session, event_id: int, *, decision: str, actor: str, now: datetime
) -> ChangeEvent:
    """Store an analyst's verdict on a change event (NFR-Q-09)."""
    if decision not in (REVIEW_ACCEPTED, REVIEW_REJECTED):
        raise ValidationError(f"decision must be {REVIEW_ACCEPTED} or {REVIEW_REJECTED}")
    event = session.get(ChangeEvent, event_id)
    if event is None:
        raise NotFoundError(f"change event {event_id} not found")
    event.review_decision = decision
    event.reviewed_by = actor
    event.reviewed_at = now
    session.flush()
    return event


def event_sample(session: Session, *, n: int, days: int, now: datetime) -> list[ChangeEvent]:
    """Random unreviewed change events of the last `days` for manual review (NFR-Q-09)."""
    return list(
        session.execute(
            select(ChangeEvent)
            .where(
                ChangeEvent.detected_at >= now - timedelta(days=days),
                ChangeEvent.review_decision.is_(None),
            )
            .order_by(func.random())
            .limit(max(1, n))
        ).scalars()
    )


def event_sample_csv(session: Session, events: Sequence[ChangeEvent]) -> str:
    """CSV with the evidence pointer (scan run → artefact prefix) for each sampled event."""
    import csv
    import io

    runs = {
        r.id: r
        for r in session.execute(
            select(ScanRun).where(ScanRun.id.in_({e.scan_run_id for e in events}))
        ).scalars()
    }
    hosts = {
        h.id: h.hostname
        for h in session.execute(
            select(Host).where(Host.id.in_({e.host_id for e in events}))
        ).scalars()
    }
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(
        [
            "event_id",
            "hostname",
            "event_type",
            "entity_type",
            "entity_id",
            "old_value",
            "new_value",
            "detected_at",
            "scan_run_id",
            "artifact_prefix",
            "decision",
        ]
    )
    for e in events:
        run = runs.get(e.scan_run_id)
        w.writerow(
            [
                e.id,
                hosts.get(e.host_id, ""),
                e.event_type.value,
                e.entity_type,
                e.entity_id or "",
                e.old_value or "",
                e.new_value or "",
                e.detected_at.isoformat(),
                str(e.scan_run_id),
                (run.artifact_prefix if run and run.artifact_prefix else ""),
                "",
            ]
        )
    return buf.getvalue()


def summary_lines(d: QualityDashboard) -> list[str]:
    lines = [f"quality {d.since.date()} … {d.until.date()}"]
    if d.overall:
        o = d.overall
        lines.append(
            f"walks {o.walks}: to checkout {o.to_checkout * 100:.1f}%, "
            f"to payment step {o.to_payment * 100:.1f}%, blocked {o.blocked * 100:.1f}%"
        )
    for o in d.outcomes[:10]:
        lines.append(
            f"  {o.platform_id}: {o.walks} walks, checkout {o.to_checkout * 100:.0f}%, "
            f"payment {o.to_payment * 100:.0f}%, blocked {o.blocked * 100:.0f}%"
        )
    lines.append(f"provider confidence: {d.provider_confidence}; methods: {d.method_confidence}")
    f = d.freshness
    lines.append(
        f"freshness: {f.profiles} profiles; light median {f.light_median_days} d, "
        f"stale {f.light_stale_share * 100:.0f}%, never {f.light_never}; checkout median "
        f"{f.checkout_median_days} d, stale {f.checkout_stale_share * 100:.0f}%, "
        f"never {f.checkout_never}"
    )
    lines.append(
        "events/day: "
        + ", ".join(f"{e.day.date()} {e.count}{'!' if e.spike else ''}" for e in d.events_per_day)
    )
    er = d.event_review
    lines.append(
        f"event review: {er.reviewed} reviewed, {er.rejected} rejected"
        + (f" ({er.rejected_share * 100:.1f}%)" if er.rejected_share is not None else "")
    )
    return lines
