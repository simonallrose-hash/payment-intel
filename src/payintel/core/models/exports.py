"""`export_job`, `canary` (4.10, FR-AB-04)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from payintel.core.models._types import enum_column
from payintel.core.models.base import Base, ExportFormat, ExportStatus, ExportType


class ExportJob(Base):
    __tablename__ = "export_job"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    type: Mapped[ExportType] = mapped_column(
        enum_column(ExportType, name="export_type"), nullable=False
    )
    format: Mapped[ExportFormat] = mapped_column(
        enum_column(ExportFormat, name="export_format"), nullable=False
    )
    params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    status: Mapped[ExportStatus] = mapped_column(
        enum_column(ExportStatus, name="export_status"), nullable=False
    )
    rows: Mapped[int | None] = mapped_column(Integer, nullable=True)
    file_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    watermark_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, default=uuid.uuid4
    )
    canary_ids: Mapped[list[int]] = mapped_column(
        ARRAY(BigInteger), nullable=False, default=list, server_default=text("'{}'")
    )
    requested_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    download_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )


class Canary(Base):
    """Synthetic domain unique to an organisation + export (FR-EX-05, FR-AB-04)."""

    __tablename__ = "canary"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    domain: Mapped[str] = mapped_column(String(253), nullable=False, unique=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    export_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("export_job.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    last_hit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
