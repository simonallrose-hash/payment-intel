"""`gold_label`: the hand-labelled gold set (FR-QA-01)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint
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
