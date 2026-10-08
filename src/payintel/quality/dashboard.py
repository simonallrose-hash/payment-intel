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

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median
from typing import Any

from clickhouse_connect.driver.client import Client
from sqlalchemy import func, literal_column, select
from sqlalchemy.orm import Session

from payintel.core.models.base import ScanStatus
from payintel.core.models.store import ChangeEvent, StorePaymentMethod, StoreProfile, StoreProvider

REACHED_CHECKOUT = frozenset(
    {ScanStatus.REACHED_CHECKOUT.value, ScanStatus.REACHED_PAYMENT_STEP.value}
)
SPIKE_MULTIPLIER = 3.0
SPIKE_MIN_EVENTS = 10


@dataclass(frozen=True)
class PlatformOutcome:
    platform_id: str
    walks: int
    to_checkout: float
    to_payment: float
    blocked: float


@dataclass(frozen=True)
class Freshness:
    profiles: int
    light_median_days: float | None
    light_stale_share: float  # older than `light_cycle_days`
    light_never: int
    checkout_median_days: float | None
    checkout_stale_share: float  # older than `checkout_cycle_days`
    checkout_never: int


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

    def as_dict(self) -> dict[str, Any]:
        return {
            "since": self.since.isoformat(),
            "until": self.until.isoformat(),
            "outcomes": [o.__dict__ for o in self.outcomes],
            "overall": self.overall.__dict__ if self.overall else None,
            "provider_confidence": self.provider_confidence,
            "method_confidence": self.method_confidence,
            "freshness": self.freshness.__dict__,
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


def freshness(
    session: Session, *, now: datetime, light_cycle_days: int, checkout_cycle_days: int
) -> Freshness:
    rows = session.execute(
        select(StoreProfile.last_light_scan_at, StoreProfile.last_checkout_scan_at)
    ).all()
    light_ages = [(now - t).total_seconds() / 86400 for t, _ in rows if t is not None]
    checkout_ages = [(now - t).total_seconds() / 86400 for _, t in rows if t is not None]
    n = len(rows)
    return Freshness(
        profiles=n,
        light_median_days=round(median(light_ages), 2) if light_ages else None,
        light_stale_share=(
            round(sum(a > light_cycle_days for a in light_ages) / len(light_ages), 4)
            if light_ages
            else 0.0
        ),
        light_never=n - len(light_ages),
        checkout_median_days=round(median(checkout_ages), 2) if checkout_ages else None,
        checkout_stale_share=(
            round(sum(a > checkout_cycle_days for a in checkout_ages) / len(checkout_ages), 4)
            if checkout_ages
            else 0.0
        ),
        checkout_never=n - len(checkout_ages),
    )


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
    checkout_cycle_days: int = 30,
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
        ),
        events_per_day=per_day,
        spikes=[d for d in per_day if d.spike],
    )


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
    return lines
