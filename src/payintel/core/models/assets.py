"""External JavaScript assets deduplicated by SHA-256 (FR-LS-03)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from payintel.core.models.base import Base


class JsAsset(Base):
    """One distinct script body; stored once in S3 under `s3_key`."""

    __tablename__ = "js_asset"

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    s3_key: Mapped[str] = mapped_column(String(256), nullable=False)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class HostJsAsset(Base):
    """`hash → hosts` relation (FR-LS-03)."""

    __tablename__ = "host_js_asset"
    __table_args__ = (
        UniqueConstraint("host_id", "sha256", name="uq_host_js_asset_host_id_sha256"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    host_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("host.id", ondelete="CASCADE"), nullable=False
    )
    sha256: Mapped[str] = mapped_column(
        String(64), ForeignKey("js_asset.sha256", ondelete="CASCADE"), nullable=False, index=True
    )
    src_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
