"""`gold_label` (FR-QA-01), `quality_alert` (FR-QA-06), `finding_review` (FR-QA-05)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from payintel.core.models._types import enum_column
from payintel.core.models.base import Base, GoldEntityType


class GoldLabel(Base):
    __tablename__ = "gold_label"
    __table_args__ = (
        UniqueConstraint("host_id", "entity_type", "entity_id", name="uq_gold_label_host_entity"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    host_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("host.id", ondelete="CASCADE"), nullable=False, index=True
    )
    entity_type: Mapped[GoldEntityType] = mapped_column(
        enum_column(GoldEntityType, name="gold_entity_type"), nullable=False
    )
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False)
    present: Mapped[bool] = mapped_column(Boolean, nullable=False)
    labeled_by: Mapped[str] = mapped_column(String(128), nullable=False)
    labeled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class QualityAlert(Base):
    """An alert raised by the quality jobs (FR-QA-06: stop-reason growth, `other` share)."""

    __tablename__ = "quality_alert"
    __table_args__ = (Index("ix_quality_alert_detected_at", "detected_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    subject: Mapped[str] = mapped_column(String(128), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    baseline: Mapped[float | None] = mapped_column(Float, nullable=True)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    window_days: Mapped[int] = mapped_column(nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_by: Mapped[str | None] = mapped_column(String(128), nullable=True)


class FindingReview(Base):
    """FR-QA-05: an analyst's decision on one finding (provider / payment method of a store)."""

    __tablename__ = "finding_review"
    __table_args__ = (
        UniqueConstraint(
            "host_id", "entity_type", "entity_id", name="uq_finding_review_host_entity"
        ),
        Index("ix_finding_review_reviewed_at", "reviewed_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    host_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("host.id", ondelete="CASCADE"), nullable=False
    )
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="confirmed | rejected"
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, comment="Snapshot of the observations shown to the analyst"
    )
    reviewed_by: Mapped[str] = mapped_column(String(128), nullable=False)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
