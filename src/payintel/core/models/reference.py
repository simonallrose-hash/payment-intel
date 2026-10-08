"""Reference dictionaries: `provider`, `payment_method`, `platform`, `vertical` (FR-NR-01..04).

Every row carries `version` and `updated_at`; changes go through
`payintel.core.reference_loader` which bumps the version and writes an audit
entry (FR-NR-04). Rows are never deleted, only marked `deprecated`.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from payintel.core.models._types import enum_column
from payintel.core.models.base import (
    Base,
    PaymentMethodType,
    ProviderRole,
    ReferenceStatus,
)


class _Versioned:
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class Provider(_Versioned, Base):
    """PSP / wallet / BNPL / orchestrator reference (FR-NR-01)."""

    __tablename__ = "provider"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    aliases: Mapped[list[str]] = mapped_column(
        ARRAY(String(128)), nullable=False, default=list, server_default=text("'{}'")
    )
    role: Mapped[ProviderRole] = mapped_column(
        enum_column(ProviderRole, name="provider_role"), nullable=False
    )
    owner_company: Mapped[str | None] = mapped_column(String(256), nullable=True)
    parent_provider_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("provider.id", ondelete="RESTRICT"), nullable=True
    )
    countries: Mapped[list[str]] = mapped_column(
        ARRAY(String(2)), nullable=False, default=list, server_default=text("'{}'")
    )
    website: Mapped[str | None] = mapped_column(String(256), nullable=True)
    status: Mapped[ReferenceStatus] = mapped_column(
        enum_column(ReferenceStatus, name="reference_status"),
        nullable=False,
        default=ReferenceStatus.ACTIVE,
        server_default=ReferenceStatus.ACTIVE.value,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class PaymentMethod(_Versioned, Base):
    """Payment-method taxonomy row (FR-NR-02, 5.3)."""

    __tablename__ = "payment_method"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    type: Mapped[PaymentMethodType] = mapped_column(
        enum_column(PaymentMethodType, name="payment_method_type"), nullable=False
    )
    scheme_or_brand: Mapped[str | None] = mapped_column(String(128), nullable=True)
    regions: Mapped[list[str]] = mapped_column(
        ARRAY(String(16)), nullable=False, default=list, server_default=text("'{}'")
    )
    default_provider_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("provider.id", ondelete="RESTRICT"), nullable=True
    )
    status: Mapped[ReferenceStatus] = mapped_column(
        enum_column(ReferenceStatus, name="reference_status"),
        nullable=False,
        default=ReferenceStatus.ACTIVE,
        server_default=ReferenceStatus.ACTIVE.value,
    )


class Platform(_Versioned, Base):
    """E-commerce platform reference (FR-NR-03)."""

    __tablename__ = "platform"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    parent_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("platform.id", ondelete="RESTRICT"), nullable=True
    )
    status: Mapped[ReferenceStatus] = mapped_column(
        enum_column(ReferenceStatus, name="reference_status"),
        nullable=False,
        default=ReferenceStatus.ACTIVE,
        server_default=ReferenceStatus.ACTIVE.value,
    )


class Vertical(_Versioned, Base):
    """Simplified 20-vertical taxonomy (FR-NR-03, FR-DT-10)."""

    __tablename__ = "vertical"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    parent_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("vertical.id", ondelete="RESTRICT"), nullable=True
    )
    keywords: Mapped[list[str]] = mapped_column(
        ARRAY(String(64)), nullable=False, default=list, server_default=text("'{}'")
    )
    status: Mapped[ReferenceStatus] = mapped_column(
        enum_column(ReferenceStatus, name="reference_status"),
        nullable=False,
        default=ReferenceStatus.ACTIVE,
        server_default=ReferenceStatus.ACTIVE.value,
    )
