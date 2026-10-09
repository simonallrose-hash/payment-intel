"""Profile `c1_basic` (FR-KYC-05): the public shape of a store without evidence.

No `third_party_hosts`, `plugins`, `platform.version`, scores or scan
internals exist on these models (FR-API-08, AS-23).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from payintel.api.schemas.common import (
    CheckoutInfo,
    CountryRef,
    MethodRef,
    PlatformRef,
    ProviderRef,
    Strict,
)


class StoreBasic(Strict):
    domain: str
    as_of: datetime
    confidence: str | None = Field(default=None, description="overall = platform confidence")
    coverage: str | None = None
    platform: PlatformRef | None = None
    country: CountryRef | None = None
    checkout: CheckoutInfo
    providers: list[ProviderRef] = Field(default_factory=list)
    payment_methods: list[MethodRef] = Field(default_factory=list)
    methodology_url: str


class ChangeBasic(Strict):
    domain: str
    type: str
    entity: str | None = None
    detected_at: datetime
