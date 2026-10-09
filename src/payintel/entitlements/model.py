"""Who is asking and what their organisation is entitled to (FR-API-06, FR-KYC-05).

A `Principal` is the authenticated caller (API key, portal user or staff
member). A `Grant` is the organisation's resolved contractual rights for this
request: segment, field profile, quotas, allowed IPs. Both are immutable value
objects; the database rows they came from are not carried around.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date

from payintel.core.models.base import FieldProfile, Role

# Scopes an API key may carry (FR-API-02). Every /v1 endpoint names one.
SCOPES: frozenset[str] = frozenset(
    {
        "stores:read",
        "changes:read",
        "stats:read",
        "watchlists:write",
        "webhooks:write",
        "exports:write",
        "usage:read",
    }
)

STAFF_ROLES: frozenset[Role] = frozenset(
    {Role.STAFF_SUPPORT, Role.STAFF_ANALYST, Role.STAFF_COMPLIANCE, Role.STAFF_ADMIN}
)
ORG_ROLES: frozenset[Role] = frozenset(
    {Role.ORG_VIEWER, Role.ORG_ANALYST, Role.ORG_ADMIN, Role.API_CLIENT}
)

# Portal roles ranked for "at least" checks (3.2): viewer < analyst < admin.
_ORG_RANK: dict[Role, int] = {Role.ORG_VIEWER: 1, Role.ORG_ANALYST: 2, Role.ORG_ADMIN: 3}


@dataclass(frozen=True)
class Principal:
    """The authenticated caller."""

    kind: str  # "api_key" | "user" | "staff"
    org_id: uuid.UUID | None
    role: Role
    user_id: uuid.UUID | None = None
    api_key_id: uuid.UUID | None = None
    scopes: frozenset[str] = frozenset()
    ip: str | None = None
    email: str | None = None

    @property
    def is_staff(self) -> bool:
        return self.role in STAFF_ROLES

    @property
    def actor(self) -> str:
        """Audit-log actor string (FR-AB-01)."""
        who = self.email or (str(self.api_key_id) if self.api_key_id else "anonymous")
        return f"{self.role.value}:{who}"

    def has_scope(self, scope: str) -> bool:
        if self.kind != "api_key":
            return True  # portal users are limited by role, not scopes
        return scope in self.scopes

    def role_at_least(self, role: Role) -> bool:
        if self.is_staff:
            return True
        if self.role == Role.API_CLIENT:
            return role == Role.API_CLIENT
        return _ORG_RANK.get(self.role, 0) >= _ORG_RANK.get(role, 99)


@dataclass(frozen=True)
class Grant:
    """Resolved entitlements of one organisation for the current request."""

    org_id: uuid.UUID
    contract_id: uuid.UUID
    product: str
    profile: FieldProfile
    countries: frozenset[str]
    platforms: frozenset[str]
    api_rps: int
    daily_records: int
    monthly_records: int
    export_max_rows: int
    export_schedule: str
    watchlist_limit: int
    allowed_ips: tuple[str, ...]
    contract_ends_on: date
    allowed_purposes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def unrestricted_countries(self) -> bool:
        return not self.countries

    @property
    def unrestricted_platforms(self) -> bool:
        return not self.platforms


class DenialCode:
    """Machine-readable reasons for 403 (FR-API-06 "понятный код ошибки")."""

    ORG_NOT_ACTIVE = "org_not_active"
    CONTRACT_NOT_IN_TERM = "contract_not_in_term"
    NO_ENTITLEMENT = "no_entitlement"
    IP_NOT_ALLOWED = "ip_not_allowed"
    SCOPE_MISSING = "scope_missing"
    ROLE_FORBIDDEN = "role_forbidden"
    OUTSIDE_SEGMENT = "outside_segment"
    FIELD_PROFILE = "field_profile_insufficient"
    C2_DISABLED = "c2_disabled"
    KEY_REVOKED = "key_revoked"
    KEY_EXPIRED = "key_expired"
    OTHER_ORG = "other_organization"
    WATCHLIST_LIMIT = "watchlist_limit_exceeded"
    EXPORT_LIMIT = "export_limit_exceeded"
