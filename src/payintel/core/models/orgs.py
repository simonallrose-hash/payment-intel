"""`organization`, `kyc_record`, `contract`, `entitlement` (4.14)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from payintel.core.models._types import enum_column
from payintel.core.models.base import Base, FieldProfile, KycDecision, OrgStatus, Product


class Organization(Base):
    __tablename__ = "organization"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    legal_name: Mapped[str] = mapped_column(String(256), nullable=False)
    reg_number: Mapped[str | None] = mapped_column(String(128), nullable=True)
    country: Mapped[str] = mapped_column(String(2), nullable=False)
    status: Mapped[OrgStatus] = mapped_column(
        enum_column(OrgStatus, name="org_status"),
        nullable=False,
        default=OrgStatus.APPLIED,
        server_default=OrgStatus.APPLIED.value,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    restricted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="FR-AB-03: API keys refused until compliance lifts the restriction",
    )
    restricted_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)


class KycRecord(Base):
    """KYC dossier (FR-KYC-02); one per organisation."""

    __tablename__ = "kyc_record"
    __table_args__ = (
        Index(
            "ix_kyc_record_next_review_at",
            "next_review_at",
            postgresql_where=text("next_review_at IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    website: Mapped[str | None] = mapped_column(String(256), nullable=True)
    beneficiaries: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    contact_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    contact_title: Mapped[str | None] = mapped_column(String(128), nullable=True)
    purpose: Mapped[str | None] = mapped_column(String(64), nullable=True)
    documents: Mapped[list[str]] = mapped_column(
        ARRAY(String(512)), nullable=False, default=list, server_default=text("'{}'")
    )
    sanctions_result: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sanctions_source: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sanctions_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    video_call_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision: Mapped[KycDecision | None] = mapped_column(
        enum_column(KycDecision, name="kyc_decision"), nullable=True
    )
    decided_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_review_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="FR-KYC-06: re-KYC due date (12 months after approval, at once on "
        "beneficiary change)",
    )
    reminder_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="FR-KYC-06: when the 30-day reminder for the current due date went out",
    )
    sanctions_details: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="FR-KYC-03: OpenSanctions hits per query (id, caption, score, datasets)",
    )


class Contract(Base):
    """FR-KYC-04."""

    __tablename__ = "contract"
    __table_args__ = (UniqueConstraint("number", name="uq_contract_number"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.id", ondelete="CASCADE"), nullable=False
    )
    number: Mapped[str] = mapped_column(String(64), nullable=False)
    product: Mapped[Product] = mapped_column(enum_column(Product, name="product"), nullable=False)
    starts_on: Mapped[date] = mapped_column(Date, nullable=False)
    ends_on: Mapped[date] = mapped_column(Date, nullable=False)
    allowed_purposes: Mapped[list[str]] = mapped_column(
        ARRAY(String(64)), nullable=False, default=list, server_default=text("'{}'")
    )
    file_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class Entitlement(Base):
    """Contractual access rights (FR-KYC-05); one per contract."""

    __tablename__ = "entitlement"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    contract_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contract.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    countries: Mapped[list[str]] = mapped_column(
        ARRAY(String(2)), nullable=False, default=list, server_default=text("'{}'")
    )
    platforms: Mapped[list[str]] = mapped_column(
        ARRAY(String(64)), nullable=False, default=list, server_default=text("'{}'")
    )
    field_profile: Mapped[FieldProfile] = mapped_column(
        enum_column(FieldProfile, name="field_profile"), nullable=False
    )
    api_rps: Mapped[int] = mapped_column(Integer, nullable=False)
    daily_records: Mapped[int] = mapped_column(Integer, nullable=False)
    monthly_records: Mapped[int] = mapped_column(Integer, nullable=False)
    export_max_rows: Mapped[int] = mapped_column(Integer, nullable=False)
    export_schedule: Mapped[str] = mapped_column(
        String(32), nullable=False, default="monthly", server_default="monthly"
    )
    watchlist_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    allowed_ips: Mapped[list[str]] = mapped_column(
        ARRAY(String(64)), nullable=False, default=list, server_default=text("'{}'")
    )
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
