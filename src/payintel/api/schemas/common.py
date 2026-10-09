"""Envelope and shared primitives of every /v1 answer (FR-API-05, FR-API-10)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

Confidence = Literal["high", "medium", "low"]


class Strict(BaseModel):
    """Base for client-facing schemas: unknown attributes are an error, not a leak."""

    model_config = ConfigDict(extra="forbid", from_attributes=False)


class Problem(Strict):
    """RFC 9457 problem details (docs/api.md)."""

    type: str = "about:blank"
    title: str
    status: int
    detail: str | None = None
    code: str
    reason: str | None = None
    instance: str | None = None


class PlatformRef(Strict):
    id: str
    confidence: Confidence | None = None


class CountryRef(Strict):
    code: str
    confidence: Confidence | None = None


class VerticalRef(Strict):
    id: str
    confidence: Confidence | None = None


class CheckoutInfo(Strict):
    status: str | None = None
    coverage: str | None = None


class CheckoutInfoFull(CheckoutInfo):
    acquirer_hidden: bool = False
    checkout_country: str | None = None


class ProviderRef(Strict):
    id: str
    name: str
    role: str
    confidence: Confidence
    first_seen: date
    last_seen: date


class Evidence(Strict):
    signal_type: str
    value: str
    page_type: str


class ProviderRefFull(ProviderRef):
    active_on_checkout: bool = False
    evidence: list[Evidence] = Field(default_factory=list)


class MethodRef(Strict):
    id: str
    type: str
    confidence: Confidence


class MethodRefFull(MethodRef):
    provider_id: str | None = None
    first_seen: date
    last_seen: date


class ProviderOut(Strict):
    id: str
    name: str
    role: str
    owner_company: str | None = None
    countries: list[str] = Field(default_factory=list)
    website: str | None = None
    status: str


class PaymentMethodOut(Strict):
    id: str
    name: str
    type: str
    scheme_or_brand: str | None = None
    regions: list[str] = Field(default_factory=list)
    default_provider_id: str | None = None


class MarketShareCell(Strict):
    country: str | None
    platform_id: str | None
    provider_id: str
    stores: int
    share: float
    ci_low: float
    ci_high: float


class MarketShareOut(Strict):
    as_of: datetime
    total_stores: int
    min_cell_size: int
    cells: list[MarketShareCell]
    methodology_url: str


T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    model_config = ConfigDict(extra="forbid")

    items: list[T]
    next_cursor: str | None = None
    as_of: datetime
    methodology_url: str


class WatchlistOut(Strict):
    id: int
    name: str
    items: int
    created_at: datetime


class WatchlistIn(Strict):
    name: str = Field(min_length=1, max_length=128)
    domains: list[str] = Field(default_factory=list)


class WatchlistItemsIn(Strict):
    domains: list[str] = Field(min_length=1)


class WatchlistItemsOut(Strict):
    added: int
    skipped: int
    total: int


class WebhookOut(Strict):
    id: int
    url: str
    enabled: bool
    created_at: datetime


class WebhookCreated(WebhookOut):
    secret: str  # shown once (FR-AL-04)


class WebhookIn(Strict):
    url: str = Field(pattern=r"^https://", max_length=1024)
    enabled: bool = True


class WebhookUpdate(Strict):
    url: str | None = Field(default=None, pattern=r"^https://", max_length=1024)
    enabled: bool | None = None


class ExportIn(Strict):
    type: Literal["full_snapshot", "increment", "market_aggregates"]
    format: Literal["parquet", "csv"] = "parquet"
    since: date | None = None  # for `increment`
    countries: list[str] = Field(default_factory=list)
    platforms: list[str] = Field(default_factory=list)


class ExportOut(Strict):
    id: str
    type: str
    format: str
    status: str
    rows: int | None = None
    download_url: str | None = None
    expires_at: datetime | None = None
    created_at: datetime
    finished_at: datetime | None = None


class UsageOut(Strict):
    org_id: str
    day_records: int
    month_records: int
    daily_limit: int
    monthly_limit: int
    api_rps: int
    requests_last_24h: int
    as_of: datetime
