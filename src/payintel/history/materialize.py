"""Materialise the current store state in Postgres from a light scan (FR-HI-02, FR-HI-05).

Light scans only ever produce `low`-confidence provider findings that are not
`active_on_checkout` (FR-DT-04/05); they must never overwrite a `high` or
`medium` row written by a checkout scan, and never set `active_on_checkout`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.core.models.base import ConfidenceLevel, ProviderRole, ScanType
from payintel.core.models.store import StoreProfile, StoreProvider

_RANK = {ConfidenceLevel.LOW: 0, ConfidenceLevel.MEDIUM: 1, ConfidenceLevel.HIGH: 2}


@dataclass(frozen=True)
class ProviderObservation:
    provider_id: str
    role: ProviderRole
    confidence: ConfidenceLevel
    score: float
    active_on_checkout: bool


@dataclass(frozen=True)
class ProfileUpdate:
    platform_id: str | None
    platform_confidence: ConfidenceLevel | None
    platform_version: str | None
    country: str | None
    country_confidence: ConfidenceLevel | None
    currency: str | None
    traffic_rank: int | None


def upsert_profile(
    session: Session,
    host_id: int,
    upd: ProfileUpdate,
    *,
    scanned_at: datetime,
    scan_type: ScanType = ScanType.LIGHT,
) -> StoreProfile:
    profile = session.get(StoreProfile, host_id)
    if profile is None:
        profile = StoreProfile(host_id=host_id)
        session.add(profile)
    # A light scan (homepage, generator tags) is the authority on the platform; a checkout
    # scan only fills a gap or confirms with at least the same confidence.
    platform_wins = upd.platform_id is not None and (
        scan_type == ScanType.LIGHT
        or profile.platform_id is None
        or profile.platform_confidence is None
        or (
            upd.platform_confidence is not None
            and _RANK[upd.platform_confidence] >= _RANK[profile.platform_confidence]
        )
    )
    if platform_wins:
        profile.platform_id = upd.platform_id
        profile.platform_confidence = upd.platform_confidence
        if upd.platform_version:
            profile.platform_version = upd.platform_version
    if upd.country is not None and (
        profile.country is None
        or profile.country_confidence is None
        or upd.country_confidence is None
        or _RANK[upd.country_confidence] >= _RANK[profile.country_confidence]
    ):
        profile.country = upd.country
        profile.country_confidence = upd.country_confidence
    if upd.currency:
        profile.currency = upd.currency
    if upd.traffic_rank is not None:
        profile.traffic_rank = upd.traffic_rank
    if scan_type == ScanType.LIGHT:
        profile.last_light_scan_at = scanned_at
    else:
        profile.last_checkout_scan_at = scanned_at
    profile.updated_at = scanned_at
    session.flush()
    return profile


def upsert_providers(
    session: Session, host_id: int, observations: list[ProviderObservation], *, scanned_at: datetime
) -> tuple[int, int]:
    """Returns (inserted, confirmed)."""
    today = scanned_at.date()
    existing = {
        p.provider_id: p
        for p in session.execute(
            select(StoreProvider).where(StoreProvider.host_id == host_id)
        ).scalars()
    }
    inserted = confirmed = 0
    for obs in observations:
        row = existing.get(obs.provider_id)
        if row is None:
            session.add(
                StoreProvider(
                    host_id=host_id,
                    provider_id=obs.provider_id,
                    role=obs.role,
                    confidence=obs.confidence,
                    confidence_score=obs.score,
                    active_on_checkout=obs.active_on_checkout,
                    first_seen=today,
                    last_seen=today,
                    confirmations=1,
                    misses=0,
                )
            )
            inserted += 1
            continue
        row.last_seen = today
        row.confirmations += 1
        row.misses = 0
        if _RANK[obs.confidence] >= _RANK[row.confidence]:
            row.confidence = obs.confidence
            row.confidence_score = max(row.confidence_score, obs.score)
        if obs.active_on_checkout:
            row.active_on_checkout = True
        confirmed += 1
    session.flush()
    return inserted, confirmed
