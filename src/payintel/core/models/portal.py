"""Stage 3 tables: portal sessions, GDPR requests, report jobs, worker cursors.

`portal_session` is server-side so that idle expiry (FR-UI-04) and revocation
are enforced by the database, not by cookie contents; the cookie carries only
a random token whose SHA-256 is stored here. `dsar_request` is the GDPR
request journal (FR-OO-03). `report_job` records C Report builds (FR-RP-*).
`system_cursor` keeps the last processed `change_event.id` for the alert
dispatcher so restarts neither skip nor duplicate deliveries.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from payintel.core.models._types import enum_column
from payintel.core.models.base import Base, DsarKind, DsarStatus, ReportStatus


class PortalSession(Base):
    __tablename__ = "portal_session"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user_account.id", ondelete="CASCADE"), nullable=False
    )
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)
    totp_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    ip: Mapped[str | None] = mapped_column(INET, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DsarRequest(Base):
    """FR-OO-03: data-subject requests with a 30-day deadline."""

    __tablename__ = "dsar_request"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[DsarKind] = mapped_column(enum_column(DsarKind, name="dsar_kind"), nullable=False)
    subject: Mapped[str] = mapped_column(String(256), nullable=False)
    contact: Mapped[str] = mapped_column(String(254), nullable=False)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[DsarStatus] = mapped_column(
        enum_column(DsarStatus, name="dsar_status"),
        nullable=False,
        default=DsarStatus.OPEN,
        server_default=DsarStatus.OPEN.value,
    )
    handled_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)


class ReportJob(Base):
    """One C Report build (FR-RP-01…04)."""

    __tablename__ = "report_job"
    __table_args__ = (Index("uq_report_job_public_slug", "public_slug", unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[ReportStatus] = mapped_column(
        enum_column(ReportStatus, name="report_status"),
        nullable=False,
        default=ReportStatus.PENDING,
        server_default=ReportStatus.PENDING.value,
    )
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)
    org_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organization.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Recipient organisation; NULL for internal reports (FR-RP-01)",
    )
    xlsx_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    csv_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    pdf_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    summary: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    public_summary: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="FR-RP-05: aggregates without domains, built with the report",
    )
    public_slug: Mapped[str | None] = mapped_column(
        String(96),
        nullable=True,
        comment="FR-RP-05: path of the public page while published",
    )
    public_pdf_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class SystemCursor(Base):
    __tablename__ = "system_cursor"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
