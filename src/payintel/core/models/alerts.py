"""`watchlist`, `watchlist_item`, `alert_rule`, `webhook`, `delivery` (4.11)."""

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
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from payintel.core.models._types import enum_column
from payintel.core.models.base import Base, DeliveryStatus


class Watchlist(Base):
    __tablename__ = "watchlist"
    __table_args__ = (UniqueConstraint("org_id", "name", name="uq_watchlist_org_id_name"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class WatchlistItem(Base):
    __tablename__ = "watchlist_item"
    __table_args__ = (
        UniqueConstraint("watchlist_id", "domain", name="uq_watchlist_item_watchlist_id_domain"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    watchlist_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("watchlist.id", ondelete="CASCADE"), nullable=False
    )
    domain: Mapped[str] = mapped_column(String(253), nullable=False, index=True)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class AlertRule(Base):
    """FR-AL-03: event types, provider, method, minimum confidence."""

    __tablename__ = "alert_rule"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.id", ondelete="CASCADE"), nullable=False
    )
    watchlist_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("watchlist.id", ondelete="CASCADE"), nullable=True
    )
    event_types: Mapped[list[str]] = mapped_column(ARRAY(String(48)), nullable=False)
    provider_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    method_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    min_confidence: Mapped[str] = mapped_column(
        String(8), nullable=False, default="medium", server_default="medium"
    )
    channel: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="telegram | webhook (FR-AL-04; no e-mail, AS-19)"
    )
    webhook_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("webhook.id", ondelete="SET NULL"), nullable=True
    )
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    digest: Mapped[str] = mapped_column(
        String(16), nullable=False, default="daily", server_default="daily"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class Webhook(Base):
    """Webhook endpoint with HMAC-SHA256 secret (FR-AL-04)."""

    __tablename__ = "webhook"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.id", ondelete="CASCADE"), nullable=False
    )
    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    secret_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class Delivery(Base):
    """Delivery log: 5 attempts within 24 h (FR-AL-04)."""

    __tablename__ = "delivery"
    __table_args__ = (
        Index(
            "uq_delivery_rule_event",
            "alert_rule_id",
            "change_event_id",
            unique=True,
            postgresql_where=text("change_event_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    alert_rule_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("alert_rule.id", ondelete="CASCADE"), nullable=False
    )
    change_event_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("change_event.id", ondelete="SET NULL"), nullable=True
    )
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[DeliveryStatus] = mapped_column(
        enum_column(DeliveryStatus, name="delivery_status"), nullable=False
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
