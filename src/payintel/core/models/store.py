"""Current store state in Postgres (FR-HI-02) and change events (FR-HI-03)."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from payintel.core.models._types import enum_column
from payintel.core.models.base import (
    Base,
    ChangeEventType,
    ConfidenceLevel,
    Coverage,
    ProviderRole,
    ScanStatus,
)


class StoreProfile(Base):
    __tablename__ = "store_profile"
    __table_args__ = (
        Index("ix_store_profile_country_platform", "country", "platform_id"),
        Index("ix_store_profile_traffic_rank", "traffic_rank"),
    )

    host_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("host.id", ondelete="CASCADE"), primary_key=True
    )
    platform_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("platform.id", ondelete="RESTRICT"), nullable=True
    )
    platform_confidence: Mapped[ConfidenceLevel | None] = mapped_column(
        enum_column(ConfidenceLevel, name="platform_confidence"), nullable=True
    )
    platform_version: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="Internal only (AS-23); never in C1 schemas"
    )
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    country_confidence: Mapped[ConfidenceLevel | None] = mapped_column(
        enum_column(ConfidenceLevel, name="country_confidence"), nullable=True
    )
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    vertical_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("vertical.id", ondelete="RESTRICT"), nullable=True
    )
    vertical_confidence: Mapped[ConfidenceLevel | None] = mapped_column(
        enum_column(ConfidenceLevel, name="vertical_confidence"), nullable=True
    )
    checkout_status: Mapped[ScanStatus | None] = mapped_column(
        enum_column(ScanStatus, name="scan_status"), nullable=True
    )
    coverage: Mapped[Coverage | None] = mapped_column(
        enum_column(Coverage, name="coverage"), nullable=True
    )
    checkout_country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    acquirer_hidden: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    last_light_scan_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_checkout_scan_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    traffic_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class StoreProvider(Base):
    """Current providers of a store with first/last seen and confirmations (FR-HI-05)."""

    __tablename__ = "store_provider"
    __table_args__ = (
        UniqueConstraint("host_id", "provider_id", name="uq_store_provider_host_id_provider_id"),
        Index("ix_store_provider_provider_id", "provider_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    host_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("host.id", ondelete="CASCADE"), nullable=False
    )
    provider_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("provider.id", ondelete="RESTRICT"), nullable=False
    )
    role: Mapped[ProviderRole] = mapped_column(
        enum_column(ProviderRole, name="provider_role"), nullable=False
    )
    confidence: Mapped[ConfidenceLevel] = mapped_column(
        enum_column(ConfidenceLevel, name="confidence_level"), nullable=False
    )
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False)
    active_on_checkout: Mapped[bool] = mapped_column(Boolean, nullable=False)
    first_seen: Mapped[date] = mapped_column(Date, nullable=False)
    last_seen: Mapped[date] = mapped_column(Date, nullable=False)
    suppressed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
        comment="FR-QA-05: rejected by an analyst, hidden from clients",
    )
    confirmations: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    misses: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
        comment="Consecutive successful scans without the entity (FR-HI-04)",
    )


class StorePaymentMethod(Base):
    __tablename__ = "store_payment_method"
    __table_args__ = (
        UniqueConstraint("host_id", "method_id", name="uq_store_payment_method_host_id_method_id"),
        Index("ix_store_payment_method_method_id", "method_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    host_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("host.id", ondelete="CASCADE"), nullable=False
    )
    method_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("payment_method.id", ondelete="RESTRICT"), nullable=False
    )
    provider_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("provider.id", ondelete="RESTRICT"), nullable=True
    )
    confidence: Mapped[ConfidenceLevel] = mapped_column(
        enum_column(ConfidenceLevel, name="confidence_level"), nullable=False
    )
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False)
    first_seen: Mapped[date] = mapped_column(Date, nullable=False)
    last_seen: Mapped[date] = mapped_column(Date, nullable=False)
    suppressed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
        comment="FR-QA-05: rejected by an analyst, hidden from clients",
    )
    confirmations: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    misses: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )


class StoreCheckoutHost(Base):
    """Third-party hosts seen on the checkout page, current state (FR-DT-11, FR-HI-02).

    Internal + C2 only except `category = 'psp'` (AS-23): the C1 schemas never
    expose this table.
    """

    __tablename__ = "store_checkout_host"
    __table_args__ = (
        UniqueConstraint(
            "host_id", "third_party_etld1", name="uq_store_checkout_host_host_id_etld1"
        ),
        Index("ix_store_checkout_host_third_party_etld1", "third_party_etld1"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    host_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("host.id", ondelete="CASCADE"), nullable=False
    )
    third_party_etld1: Mapped[str] = mapped_column(String(253), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("provider.id", ondelete="SET NULL"), nullable=True
    )
    request_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    first_seen: Mapped[date] = mapped_column(Date, nullable=False)
    last_seen: Mapped[date] = mapped_column(Date, nullable=False)
    confirmations: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    misses: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )


class ChangeEvent(Base):
    __tablename__ = "change_event"
    __table_args__ = (
        Index("ix_change_event_host_id_detected_at", "host_id", "detected_at"),
        Index("ix_change_event_type_detected_at", "event_type", "detected_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    host_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("host.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[ChangeEventType] = mapped_column(
        enum_column(ChangeEventType, name="change_event_type"), nullable=False
    )
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(253), nullable=True)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    scan_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("scan_run.id", ondelete="CASCADE"), nullable=False
    )
    suppressed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
