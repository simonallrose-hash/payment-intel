"""`detection_rule` (FR-DT-01): versioned YAML rules loaded into the database at deploy."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Index, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from payintel.core.models._types import enum_column
from payintel.core.models.base import Base, PageScope, RuleTargetType, SignalType


class DetectionRule(Base):
    __tablename__ = "detection_rule"
    __table_args__ = (
        UniqueConstraint("rule_id", "version", name="uq_detection_rule_rule_id_version"),
        Index("ix_detection_rule_target", "target_type", "target_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    rule_id: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(nullable=False)
    target_type: Mapped[RuleTargetType] = mapped_column(
        enum_column(RuleTargetType, name="rule_target_type"), nullable=False
    )
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    signal_type: Mapped[SignalType] = mapped_column(
        enum_column(SignalType, name="signal_type"), nullable=False
    )
    pattern: Mapped[str] = mapped_column(Text, nullable=False)
    weight: Mapped[float] = mapped_column(Float, nullable=False)
    page_scope: Mapped[PageScope] = mapped_column(
        enum_column(PageScope, name="page_scope"), nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    needs_verification: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    source_file: Mapped[str] = mapped_column(String(256), nullable=False)
    loaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
