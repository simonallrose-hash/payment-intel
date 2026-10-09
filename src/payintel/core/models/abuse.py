"""`abuse_incident` (FR-AB-02/03), `canary_hit` (FR-AB-04), `usage_report` (FR-AB-05)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from payintel.core.models.base import Base


class AbuseIncident(Base):
    """A detector finding for `staff_compliance`; critical ones restrict the organisation."""

    __tablename__ = "abuse_incident"
    __table_args__ = (
        Index("ix_abuse_incident_org_id", "org_id"),
        Index("ix_abuse_incident_status", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.id", ondelete="CASCADE"), nullable=False
    )
    detector: Mapped[str] = mapped_column(String(48), nullable=False)
    severity: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="low|medium|high|critical"
    )
    summary: Mapped[str] = mapped_column(String(512), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="open|resolved|dismissed"
    )
    auto_restricted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)


class CanaryHit(Base):
    """One observed access to a canary domain, linked to the organisation that received it."""

    __tablename__ = "canary_hit"
    __table_args__ = (
        Index("ix_canary_hit_org_id", "org_id"),
        Index("ix_canary_hit_observed_at", "observed_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    canary_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("canary.id", ondelete="CASCADE"), nullable=False
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False, comment="dns|http|email")
    source: Mapped[str | None] = mapped_column(String(256), nullable=True)
    detail: Mapped[str | None] = mapped_column(String(512), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class UsageReport(Base):
    """Quarterly usage report per organisation (FR-AB-05)."""

    __tablename__ = "usage_report"
    __table_args__ = (UniqueConstraint("org_id", "period", name="uq_usage_report_org_period"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.id", ondelete="CASCADE"), nullable=False
    )
    period: Mapped[str] = mapped_column(String(8), nullable=False, comment="e.g. 2026-Q3")
    file_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
