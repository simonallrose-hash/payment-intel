"""Profile `c1_full` (5.4): `c1_basic` plus evidence, checkout PSP hosts and changes.

`checkout_psp_hosts` contains only hosts of category `psp` (FR-DT-11); all
other third-party hosts stay internal (AS-23).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from payintel.api.schemas.common import (
    CheckoutInfoFull,
    CountryRef,
    MethodRefFull,
    PlatformRef,
    ProviderRefFull,
    Strict,
    VerticalRef,
)


class RecentChange(Strict):
    type: str
    entity: str | None = None
    detected_at: datetime


class StoreFull(Strict):
    domain: str
    as_of: datetime
    confidence: str | None = None
    coverage: str | None = None
    platform: PlatformRef | None = None
    country: CountryRef | None = None
    currency: str | None = None
    vertical: VerticalRef | None = None
    checkout: CheckoutInfoFull
    providers: list[ProviderRefFull] = Field(default_factory=list)
    payment_methods: list[MethodRefFull] = Field(default_factory=list)
    checkout_psp_hosts: list[str] = Field(default_factory=list)
    recent_changes: list[RecentChange] = Field(default_factory=list)
    traffic_rank: int | None = None
    last_light_scan_at: datetime | None = None
    last_checkout_scan_at: datetime | None = None
    methodology_url: str


class ChangeFull(Strict):
    domain: str
    type: str
    entity: str | None = None
    old_value: str | None = None
    new_value: str | None = None
    detected_at: datetime
