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


class JsAssetUrl(Base):
    """One third-party script URL, fetched once by the script pool (FR-LS-03, ADR-0038).

    Light scans never download scripts from other eTLD+1s (a CDN host shared by
    thousands of shops would serialise the fleet on the 1 rps rule, ADR-0011);
    they record the URL here and the pool fetches it once, then every host that
    referenced it is linked to the body's SHA-256. `status`: pending → ok | error.
    """

    __tablename__ = "js_asset_url"

    url: Mapped[str] = mapped_column(String(2048), primary_key=True)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    sha256: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("js_asset.sha256", ondelete="SET NULL"), nullable=True
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_fetch_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class JsAssetUrlHost(Base):
    """Hosts waiting for a script URL to be fetched; rows turn into `host_js_asset`
    links when the pool has the body (FR-LS-03)."""

    __tablename__ = "js_asset_url_host"
    __table_args__ = (UniqueConstraint("url", "host_id", name="uq_js_asset_url_host"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    url: Mapped[str] = mapped_column(
        String(2048), ForeignKey("js_asset_url.url", ondelete="CASCADE"), nullable=False
    )
    host_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("host.id", ondelete="CASCADE"), nullable=False, index=True
    )
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
