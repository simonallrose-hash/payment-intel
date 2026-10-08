"""`usage_log`, `audit_log`, `feature_flag`, `optout_request` (4.15, 4.16, 4.18).

`usage_log` is range-partitioned by month (5.1); the partition DDL lives in the
Alembic migration and `payintel.core.partitions`. `audit_log` is append-only:
UPDATE/DELETE are rejected by a trigger created in the migration (FR-AB-01) and
rows are chained by `prev_hash`/`row_hash` (NFR-S-11).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from payintel.core.models._types import enum_column
from payintel.core.models.base import Base, OptoutMethod


class UsageLog(Base):
    __tablename__ = "usage_log"
    __table_args__ = (
        Index("ix_usage_log_org_id_ts", "org_id", "ts"),
        {"postgresql_partition_by": "RANGE (ts)"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    api_key_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    endpoint: Mapped[str] = mapped_column(String(128), nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    records: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    ip: Mapped[str | None] = mapped_column(INET, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_object", "object_type", "object_id"),
        Index("ix_audit_log_ts", "ts"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    object_type: Mapped[str] = mapped_column(String(64), nullable=False)
    object_id: Mapped[str] = mapped_column(String(128), nullable=False)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ip: Mapped[str | None] = mapped_column(INET, nullable=True)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    row_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)


class FeatureFlag(Base):
    """FR-ADM-05; changes are audited through `payintel.core.flags`."""

    __tablename__ = "feature_flag"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    changed_by: Mapped[str] = mapped_column(String(128), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OptoutRequest(Base):
    """FR-OO-02."""

    __tablename__ = "optout_request"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    domain: Mapped[str] = mapped_column(String(253), nullable=False, index=True)
    method: Mapped[OptoutMethod] = mapped_column(
        enum_column(OptoutMethod, name="optout_method"), nullable=False
    )
    token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    contact: Mapped[str | None] = mapped_column(String(254), nullable=True)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
