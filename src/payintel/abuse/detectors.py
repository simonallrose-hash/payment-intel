"""Usage anomaly detectors over the usage journal (FR-AB-02).

Each detector looks at one organisation's `usage_log` rows in the detection
window and returns a `Finding` with a severity. Detectors:

* `outside_segment`: share of requests refused with `outside_segment`;
* `records_spike`: records in the window above N× the mean of the previous
  `baseline_days` windows;
* `enumeration`: many `GET /v1/stores/{domain}` lookups, almost all distinct
  (sequential walking of the catalogue);
* `new_network`: requests from a /16 network never seen for this organisation
  in the previous 30 days (no GeoIP/ASN database is bundled: the prefix is the
  proxy for "new country or ASN", see ADR-0023);
* `field_probing`: repeated `field_profile_insufficient` refusals (access to
  fields outside the entitlement).

Severity `critical` is reserved for the two strongest signals and leads to
an automatic restriction of the organisation's keys (FR-AB-03).
"""

from __future__ import annotations

import ipaddress
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from payintel.core.models.audit import UsageLog
from payintel.core.settings import AbuseSettings

SEVERITIES: tuple[str, ...] = ("low", "medium", "high", "critical")
LOOKUP_ENDPOINT = "GET /v1/stores/{domain}"


@dataclass(frozen=True)
class Finding:
    detector: str
    severity: str
    summary: str
    details: dict[str, Any] = field(default_factory=dict)


def _rows(session: Session, org_id: uuid.UUID, since: datetime, until: datetime) -> list[UsageLog]:
    return list(
        session.execute(
            select(UsageLog)
            .where(UsageLog.org_id == org_id, UsageLog.ts >= since, UsageLog.ts < until)
            .order_by(UsageLog.ts)
        ).scalars()
    )


def _network(ip: str | None) -> str | None:
    if not ip:
        return None
    try:
        addr = ipaddress.ip_address(str(ip))
    except ValueError:
        return None
    bits = 16 if addr.version == 4 else 32
    return str(ipaddress.ip_network(f"{addr}/{bits}", strict=False))


def detect_org(
    session: Session, org_id: uuid.UUID, *, now: datetime, s: AbuseSettings
) -> list[Finding]:
    window = timedelta(hours=s.window_hours)
    since = now - window
    rows = _rows(session, org_id, since, now)
    out: list[Finding] = []
    if not rows:
        return out
    total = len(rows)

    # 1) requests outside the typical segment
    outside = sum(1 for r in rows if r.params.get("denial") == "outside_segment")
    if total >= s.outside_segment_min and outside / total >= s.outside_segment_share:
        out.append(
            Finding(
                "outside_segment",
                "medium",
                f"{outside} of {total} requests refused as outside_segment "
                f"({outside / total * 100:.0f}%)",
                {"requests": total, "outside_segment": outside},
            )
        )

    # 2) records spike vs the mean of the previous baseline windows
    records = sum(r.records for r in rows)
    base_since = since - timedelta(days=s.baseline_days)
    base_total = int(
        session.execute(
            select(func.coalesce(func.sum(UsageLog.records), 0)).where(
                UsageLog.org_id == org_id, UsageLog.ts >= base_since, UsageLog.ts < since
            )
        ).scalar_one()
    )
    windows = max(1.0, timedelta(days=s.baseline_days) / window)
    mean = base_total / windows
    if records >= s.records_min and records > s.records_multiplier * max(mean, 1.0):
        ratio = records / max(mean, 1.0)
        sev = "critical" if ratio >= s.critical_records_multiplier else "high"
        out.append(
            Finding(
                "records_spike",
                sev,
                f"{records} records in {s.window_hours} h vs {mean:.0f} on average "
                f"(×{ratio:.1f})",
                {"records": records, "baseline_mean": round(mean, 1), "ratio": round(ratio, 2)},
            )
        )

    # 3) sequential enumeration of domains
    lookups = [r for r in rows if r.endpoint == LOOKUP_ENDPOINT]
    domains = {r.params.get("domain") or r.request_id or str(r.id) for r in lookups}
    if lookups and len(lookups) >= s.enumeration_min_lookups:
        share = len(domains) / len(lookups)
        if share >= s.enumeration_distinct_share:
            sev = "critical" if len(lookups) >= s.critical_enumeration_lookups else "high"
            out.append(
                Finding(
                    "enumeration",
                    sev,
                    f"{len(lookups)} store lookups, {len(domains)} distinct domains "
                    f"({share * 100:.0f}%) in {s.window_hours} h",
                    {"lookups": len(lookups), "distinct": len(domains)},
                )
            )

    # 4) requests from networks never seen before
    known = {
        _network(ip)
        for ip in session.execute(
            select(UsageLog.ip.distinct()).where(
                UsageLog.org_id == org_id,
                UsageLog.ts >= now - timedelta(days=30),
                UsageLog.ts < since,
            )
        ).scalars()
    }
    known.discard(None)
    if known:
        fresh: dict[str, int] = {}
        for r in rows:
            net = _network(str(r.ip) if r.ip else None)
            if net and net not in known:
                fresh[net] = fresh.get(net, 0) + 1
        loud = {n: c for n, c in fresh.items() if c >= s.new_network_min_requests}
        if loud:
            out.append(
                Finding(
                    "new_network",
                    "low",
                    f"{sum(loud.values())} requests from {len(loud)} network(s) not seen in "
                    "the previous 30 days",
                    {"networks": loud},
                )
            )

    # 5) probing fields outside the entitlement
    probes = sum(1 for r in rows if r.params.get("denial") == "field_profile_insufficient")
    if probes >= s.field_attempts_min:
        out.append(
            Finding(
                "field_probing",
                "medium",
                f"{probes} attempts to read fields outside the entitlement",
                {"attempts": probes},
            )
        )
    return out
