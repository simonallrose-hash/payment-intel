"""Stop-reason review (FR-QA-06): distribution, weekly trend, release comparison, samples, alerts.

The data is `obs_scan_stop` in ClickHouse (one row per walk that did not reach
the payment step). This module is the M-part of FR-QA-06: the numbers behind
the admin page "Причины остановок" (stage 3 renders them), the ≤20-domain
sample with screenshot and DOM keys, its CSV, and the two alerts for
`staff_analyst`:

- `stop_reason_growth`: the share of a reason on a platform grew by
  `quality.stop_reason_alert_delta_pp` (5 p.p.) or more week over week, or
  between the previous and the current scanner/ruleset release;
- `stop_reason_other_share`: `other` is more than
  `quality.stop_reason_other_max_share` (5 %) of all stops — the taxonomy
  needs a new code (the weekly review in docs/runbook.md).

Alerts are `quality_alert` rows in PostgreSQL; an open (unacknowledged) alert
with the same kind and subject is not duplicated.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from clickhouse_connect.driver.client import Client
from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.core.models.quality import QualityAlert
from payintel.core.settings import QualitySettings
from payintel.crawl.checkout.stops import DOM_NAME, SCREENSHOT_NAME

SAMPLE_LIMIT = 20
MIN_STOPS_FOR_ALERT = 20  # a share computed on fewer stops is noise, not a trend
KIND_GROWTH = "stop_reason_growth"
KIND_OTHER = "stop_reason_other_share"


@dataclass(frozen=True)
class StopCell:
    step: str
    reason: str
    platform_id: str
    adapter: str
    count: int
    share: float  # of all stops in the window


@dataclass(frozen=True)
class WeekPoint:
    week: datetime
    reason: str
    count: int
    share: float


@dataclass(frozen=True)
class ReleaseShare:
    version: str
    stops: int
    shares: dict[str, float]  # reason → share


@dataclass(frozen=True)
class StopSample:
    scan_ts: datetime
    etld1: str
    host_id: int
    platform_id: str
    adapter: str
    step: str
    reason: str
    detail: str
    page_url: str
    scan_run_id: str
    screenshot_key: str
    dom_key: str
    scanner_version: str
    ruleset_version: str


@dataclass
class StopReview:
    since: datetime
    until: datetime
    total: int
    cells: list[StopCell]
    by_reason: dict[str, int]
    by_step: dict[str, int]
    by_platform: dict[str, int]
    other_share: float
    weekly: list[WeekPoint] = field(default_factory=list)
    releases: list[ReleaseShare] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "since": self.since.isoformat(),
            "until": self.until.isoformat(),
            "total": self.total,
            "other_share": round(self.other_share, 4),
            "by_reason": self.by_reason,
            "by_step": self.by_step,
            "by_platform": self.by_platform,
            "cells": [c.__dict__ for c in self.cells],
            "weekly": [
                {
                    "week": w.week.date().isoformat(),
                    "reason": w.reason,
                    "count": w.count,
                    "share": w.share,
                }
                for w in self.weekly
            ],
            "releases": [
                {"version": r.version, "stops": r.stops, "shares": r.shares} for r in self.releases
            ],
        }


def _window(now: datetime, days: int) -> tuple[datetime, datetime]:
    return now - timedelta(days=days), now


def distribution(
    ch: Client, *, since: datetime, until: datetime, platform_id: str | None = None
) -> list[StopCell]:
    where = "scan_ts >= %(since)s AND scan_ts < %(until)s"
    params: dict[str, Any] = {"since": since, "until": until}
    if platform_id is not None:
        where += " AND platform_id = %(platform)s"
        params["platform"] = platform_id
    rows = ch.query(
        f"SELECT stop_step, stop_reason, platform_id, adapter, count() AS n "  # noqa: S608 - fixed identifiers
        f"FROM obs_scan_stop WHERE {where} "
        "GROUP BY stop_step, stop_reason, platform_id, adapter ORDER BY n DESC, stop_reason",
        parameters=params,
    ).result_rows
    total = sum(int(r[4]) for r in rows)
    return [
        StopCell(str(r[0]), str(r[1]), str(r[2]), str(r[3]), int(r[4]), int(r[4]) / total)
        for r in rows
    ]


def weekly_trend(ch: Client, *, now: datetime, weeks: int = 8) -> list[WeekPoint]:
    since = (now - timedelta(weeks=weeks)).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = ch.query(
        "SELECT toStartOfWeek(scan_ts) AS w, stop_reason, count() AS n FROM obs_scan_stop "
        "WHERE scan_ts >= %(since)s AND scan_ts < %(until)s "
        "GROUP BY w, stop_reason ORDER BY w, n DESC",
        parameters={"since": since, "until": now},
    ).result_rows
    totals: dict[Any, int] = {}
    for w, _reason, n in rows:
        totals[w] = totals.get(w, 0) + int(n)
    out = []
    for w, reason, n in rows:
        week = w if isinstance(w, datetime) else datetime.combine(w, datetime.min.time())
        out.append(WeekPoint(week, str(reason), int(n), int(n) / totals[w]))
    return out


def release_comparison(
    ch: Client, *, since: datetime, until: datetime, by: str = "scanner_version"
) -> list[ReleaseShare]:
    """Shares per reason for each scanner (or ruleset) version seen in the window, oldest first."""
    if by not in {"scanner_version", "ruleset_version"}:
        raise ValueError("by must be scanner_version or ruleset_version")
    rows = ch.query(
        f"SELECT {by}, stop_reason, count() AS n, min(scan_ts) AS first_seen FROM obs_scan_stop "  # noqa: S608 - closed set
        f"WHERE scan_ts >= %(since)s AND scan_ts < %(until)s GROUP BY {by}, stop_reason",
        parameters={"since": since, "until": until},
    ).result_rows
    per_version: dict[str, dict[str, int]] = {}
    first: dict[str, datetime] = {}
    for version, reason, n, first_seen in rows:
        per_version.setdefault(str(version), {})[str(reason)] = int(n)
        first[str(version)] = min(first.get(str(version), first_seen), first_seen)
    out = []
    for version in sorted(per_version, key=lambda v: first[v]):
        counts = per_version[version]
        total = sum(counts.values())
        out.append(
            ReleaseShare(version, total, {r: round(c / total, 4) for r, c in counts.items()})
        )
    return out


def review(ch: Client, *, now: datetime, days: int = 7, weeks: int = 8) -> StopReview:
    since, until = _window(now, days)
    cells = distribution(ch, since=since, until=until)
    total = sum(c.count for c in cells)
    by_reason: dict[str, int] = {}
    by_step: dict[str, int] = {}
    by_platform: dict[str, int] = {}
    for c in cells:
        by_reason[c.reason] = by_reason.get(c.reason, 0) + c.count
        by_step[c.step] = by_step.get(c.step, 0) + c.count
        by_platform[c.platform_id or "(none)"] = (
            by_platform.get(c.platform_id or "(none)", 0) + c.count
        )
    return StopReview(
        since=since,
        until=until,
        total=total,
        cells=cells,
        by_reason=dict(sorted(by_reason.items(), key=lambda kv: -kv[1])),
        by_step=dict(sorted(by_step.items(), key=lambda kv: -kv[1])),
        by_platform=dict(sorted(by_platform.items(), key=lambda kv: -kv[1])),
        other_share=(by_reason.get("other", 0) / total) if total else 0.0,
        weekly=weekly_trend(ch, now=now, weeks=weeks),
        releases=release_comparison(ch, since=now - timedelta(weeks=weeks), until=until),
    )


def sample(
    ch: Client,
    *,
    step: str,
    reason: str,
    since: datetime,
    until: datetime,
    platform_id: str | None = None,
    adapter: str | None = None,
    limit: int = SAMPLE_LIMIT,
) -> list[StopSample]:
    """Up to `limit` distinct domains of a cell, newest stop first, with evidence keys."""
    where = (
        "scan_ts >= %(since)s AND scan_ts < %(until)s AND stop_step = %(step)s "
        "AND stop_reason = %(reason)s"
    )
    params: dict[str, Any] = {
        "since": since,
        "until": until,
        "step": step,
        "reason": reason,
        "limit": max(1, min(int(limit), SAMPLE_LIMIT)),
    }
    if platform_id is not None:
        where += " AND platform_id = %(platform)s"
        params["platform"] = platform_id
    if adapter is not None:
        where += " AND adapter = %(adapter)s"
        params["adapter"] = adapter
    rows = ch.query(
        "SELECT scan_ts, etld1, host_id, platform_id, adapter, stop_step, stop_reason, "  # noqa: S608 - fixed identifiers, values bound
        "stop_detail, page_url, scan_run_id, artifact_key, scanner_version, ruleset_version "
        f"FROM obs_scan_stop WHERE {where} "
        "ORDER BY scan_ts DESC LIMIT 1 BY etld1 LIMIT %(limit)s",
        parameters=params,
    ).result_rows
    out = []
    for r in rows:
        artifact_key = str(r[10])
        prefix = (
            artifact_key[: -len(SCREENSHOT_NAME)] if artifact_key.endswith(SCREENSHOT_NAME) else ""
        )
        out.append(
            StopSample(
                scan_ts=r[0],
                etld1=str(r[1]),
                host_id=int(r[2]),
                platform_id=str(r[3]),
                adapter=str(r[4]),
                step=str(r[5]),
                reason=str(r[6]),
                detail=str(r[7]),
                page_url=str(r[8]),
                scan_run_id=str(r[9]),
                screenshot_key=artifact_key,
                dom_key=f"{prefix}{DOM_NAME}" if prefix else "",
                scanner_version=str(r[11]),
                ruleset_version=str(r[12]),
            )
        )
    return out


def sample_csv(rows: Sequence[StopSample]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(
        [
            "scan_ts",
            "etld1",
            "platform_id",
            "adapter",
            "stop_step",
            "stop_reason",
            "stop_detail",
            "page_url",
            "scan_run_id",
            "screenshot_key",
            "dom_key",
            "scanner_version",
            "ruleset_version",
        ]
    )
    for s in rows:
        w.writerow(
            [
                s.scan_ts.isoformat(),
                s.etld1,
                s.platform_id,
                s.adapter,
                s.step,
                s.reason,
                s.detail,
                s.page_url,
                s.scan_run_id,
                s.screenshot_key,
                s.dom_key,
                s.scanner_version,
                s.ruleset_version,
            ]
        )
    return buf.getvalue()


# --- alerts ----------------------------------------------------------------------------------


def _shares_by_platform(cells: Sequence[StopCell]) -> dict[str, dict[str, float]]:
    counts: dict[str, dict[str, int]] = {}
    for c in cells:
        counts.setdefault(c.platform_id, {})
        counts[c.platform_id][c.reason] = counts[c.platform_id].get(c.reason, 0) + c.count
    out: dict[str, dict[str, float]] = {}
    for platform, reasons in counts.items():
        total = sum(reasons.values())
        if total < MIN_STOPS_FOR_ALERT:
            continue
        out[platform] = {r: n / total for r, n in reasons.items()}
    return out


def _open_alerts(session: Session) -> set[tuple[str, str]]:
    rows = session.execute(
        select(QualityAlert.kind, QualityAlert.subject).where(
            QualityAlert.acknowledged_at.is_(None)
        )
    ).all()
    return {(str(k), str(s)) for k, s in rows}


def detect_alerts(
    session: Session, ch: Client, *, now: datetime, s: QualitySettings
) -> list[QualityAlert]:
    """Write new `quality_alert` rows for FR-QA-06 and return them."""
    delta_pp = s.stop_reason_alert_delta_pp
    this_since, this_until = _window(now, 7)
    prev_since, prev_until = _window(now - timedelta(days=7), 7)
    current = _shares_by_platform(distribution(ch, since=this_since, until=this_until))
    previous = _shares_by_platform(distribution(ch, since=prev_since, until=prev_until))
    candidates: list[QualityAlert] = []
    for platform, shares in current.items():
        for reason, share in shares.items():
            base = previous.get(platform, {}).get(reason, 0.0) if platform in previous else None
            if base is None:
                continue
            if (share - base) * 100 >= delta_pp:
                candidates.append(
                    QualityAlert(
                        kind=KIND_GROWTH,
                        subject=f"{platform or '(none)'}:{reason}",
                        value=round(share, 4),
                        baseline=round(base, 4),
                        threshold=delta_pp,
                        window_days=7,
                        message=(
                            f"stop reason {reason} on {platform or 'unknown platform'} grew from "
                            f"{base * 100:.1f}% to {share * 100:.1f}% week over week"
                        ),
                        detected_at=now,
                    )
                )
    # release comparison: the two most recent scanner versions of the last 14 days
    releases = release_comparison(ch, since=now - timedelta(days=14), until=now)
    if len(releases) >= 2:
        before, after = releases[-2], releases[-1]
        if before.stops >= MIN_STOPS_FOR_ALERT and after.stops >= MIN_STOPS_FOR_ALERT:
            for reason, share in after.shares.items():
                base = before.shares.get(reason, 0.0)
                if (share - base) * 100 >= delta_pp:
                    candidates.append(
                        QualityAlert(
                            kind=KIND_GROWTH,
                            subject=f"release:{after.version}:{reason}",
                            value=share,
                            baseline=base,
                            threshold=delta_pp,
                            window_days=14,
                            message=(
                                f"stop reason {reason} grew from {base * 100:.1f}% "
                                f"(scanner {before.version}) to {share * 100:.1f}% "
                                f"(scanner {after.version})"
                            ),
                            detected_at=now,
                        )
                    )
    all_cells = distribution(ch, since=this_since, until=this_until)
    total = sum(c.count for c in all_cells)
    other = sum(c.count for c in all_cells if c.reason == "other")
    if total >= MIN_STOPS_FOR_ALERT and other / total > s.stop_reason_other_max_share:
        candidates.append(
            QualityAlert(
                kind=KIND_OTHER,
                subject="all",
                value=round(other / total, 4),
                baseline=None,
                threshold=s.stop_reason_other_max_share,
                window_days=7,
                message=(
                    f"`other` is {other / total * 100:.1f}% of {total} stops this week "
                    f"(target ≤ {s.stop_reason_other_max_share * 100:.0f}%): extend the taxonomy"
                ),
                detected_at=now,
            )
        )
    open_alerts = _open_alerts(session)
    created = []
    for alert in candidates:
        if (alert.kind, alert.subject) in open_alerts:
            continue
        session.add(alert)
        created.append(alert)
        open_alerts.add((alert.kind, alert.subject))
    session.flush()
    return created


def summary_lines(r: StopReview) -> list[str]:
    lines = [
        f"stops {r.since.date()} … {r.until.date()}: {r.total} "
        f"(other {r.other_share * 100:.1f}%)",
        "by reason: " + ", ".join(f"{k} {v}" for k, v in list(r.by_reason.items())[:10]),
        "by step: " + ", ".join(f"{k} {v}" for k, v in r.by_step.items()),
        "by platform: " + ", ".join(f"{k} {v}" for k, v in list(r.by_platform.items())[:10]),
    ]
    for rel in r.releases:
        top = sorted(rel.shares.items(), key=lambda kv: -kv[1])[:5]
        lines.append(
            f"release {rel.version}: {rel.stops} stops, "
            + ", ".join(f"{k} {v * 100:.0f}%" for k, v in top)
        )
    return lines
