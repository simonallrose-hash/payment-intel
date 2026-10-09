"""Segment restriction: countries and platforms of the entitlement (FR-API-06).

`segment_clause` is applied to every store query and every export; a direct
lookup of a store outside the segment is a 403 `outside_segment`, not a 404,
so the client learns the cause (FR-API-06). The country used is the store's
detected `country` (FR-DT-09), the platform is `store_profile.platform_id`.
"""

from __future__ import annotations

from sqlalchemy import ColumnElement, and_, true

from payintel.core.models.store import StoreProfile
from payintel.entitlements.check import EntitlementDenied
from payintel.entitlements.model import DenialCode, Grant


def in_segment(grant: Grant, *, country: str | None, platform_id: str | None) -> bool:
    if not grant.unrestricted_countries and (country or "").upper() not in grant.countries:
        return False
    return grant.unrestricted_platforms or (platform_id or "") in grant.platforms


def require_in_segment(grant: Grant, *, country: str | None, platform_id: str | None) -> None:
    if not in_segment(grant, country=country, platform_id=platform_id):
        raise EntitlementDenied(DenialCode.OUTSIDE_SEGMENT, "store is outside the segment")


def segment_clause(grant: Grant) -> ColumnElement[bool]:
    parts: list[ColumnElement[bool]] = []
    if not grant.unrestricted_countries:
        parts.append(StoreProfile.country.in_(sorted(grant.countries)))
    if not grant.unrestricted_platforms:
        parts.append(StoreProfile.platform_id.in_(sorted(grant.platforms)))
    if not parts:
        return true()
    return and_(*parts)


def countries_in_segment(grant: Grant, requested: list[str]) -> list[str]:
    """Narrow a requested country filter to the segment; a request outside → 403."""
    wanted = [c.upper() for c in requested]
    if grant.unrestricted_countries:
        return wanted
    outside = [c for c in wanted if c not in grant.countries]
    if outside:
        raise EntitlementDenied(
            DenialCode.OUTSIDE_SEGMENT, "country filter outside the segment", countries=outside
        )
    return wanted or sorted(grant.countries)


def platforms_in_segment(grant: Grant, requested: list[str]) -> list[str]:
    if grant.unrestricted_platforms:
        return list(requested)
    outside = [p for p in requested if p not in grant.platforms]
    if outside:
        raise EntitlementDenied(
            DenialCode.OUTSIDE_SEGMENT, "platform filter outside the segment", platforms=outside
        )
    return list(requested) or sorted(grant.platforms)
