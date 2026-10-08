"""`scan_plan`, `scan_run`, `store_account` (5.1; FR-SC-01, FR-SC-04, FR-CW-09, FR-CW-12)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
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
    Coverage,
    ScanStatus,
    ScanType,
    StoreAccountStatus,
)


class ScanPlan(Base):
    """Per-host, per-scan-type plan and queue row (FR-SC-01; leased via SKIP LOCKED, FR-SC-04)."""

    __tablename__ = "scan_plan"
    __table_args__ = (
        UniqueConstraint("host_id", "scan_type", name="uq_scan_plan_host_id_scan_type"),
        # Queue polling index: ready tasks by priority
        Index("ix_scan_plan_queue", "scan_type", "next_scan_at", "priority"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    host_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("host.id", ondelete="CASCADE"), nullable=False
    )
    scan_type: Mapped[ScanType] = mapped_column(
        enum_column(ScanType, name="scan_type"), nullable=False
    )
    priority: Mapped[float] = mapped_column(nullable=False, default=0.0, server_default=text("0"))
    next_scan_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fail_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class ScanRun(Base):
    """One scan execution (5.1 `scan_run`)."""

    __tablename__ = "scan_run"
    __table_args__ = (
        Index("ix_scan_run_host_id_started_at", "host_id", "started_at"),
        Index("ix_scan_run_status_started_at", "status", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    host_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("host.id", ondelete="CASCADE"), nullable=False
    )
    scan_type: Mapped[ScanType] = mapped_column(
        enum_column(ScanType, name="scan_type"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[ScanStatus] = mapped_column(
        enum_column(ScanStatus, name="scan_status"), nullable=False
    )
    coverage: Mapped[Coverage | None] = mapped_column(
        enum_column(Coverage, name="coverage"), nullable=True
    )
    stop_step: Mapped[str | None] = mapped_column(String(32), nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    checkout_country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    used_account: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    worker_id: Mapped[str] = mapped_column(String(128), nullable=False)
    ruleset_version: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_prefix: Mapped[str | None] = mapped_column(String(256), nullable=True)


class StoreAccount(Base):
    """Synthetic shop account, one per host, password encrypted (FR-CW-12, AS-25)."""

    __tablename__ = "store_account"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    host_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("host.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    password_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[StoreAccountStatus] = mapped_column(
        enum_column(StoreAccountStatus, name="store_account_status"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_scan_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("scan_run.id", ondelete="SET NULL"), nullable=True
    )
