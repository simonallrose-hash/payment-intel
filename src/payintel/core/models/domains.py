"""`domain`, `host`, `domain_source` (5.1; FR-DS-03..05)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, INET
from sqlalchemy.orm import Mapped, mapped_column, relationship

from payintel.core.models._types import enum_column
from payintel.core.models.base import Base, DomainSourceKind, DomainStatus


class Domain(Base):
    """Registrable domain (eTLD+1 by the Public Suffix List, FR-DS-04)."""

    __tablename__ = "domain"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    etld1: Mapped[str] = mapped_column(String(253), unique=True, nullable=False)
    tld: Mapped[str] = mapped_column(String(63), nullable=False, index=True)
    status: Mapped[DomainStatus] = mapped_column(
        enum_column(DomainStatus, name="domain_status"),
        nullable=False,
        default=DomainStatus.CANDIDATE,
        server_default=DomainStatus.CANDIDATE.value,
        index=True,
    )
    optout: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    status_reason: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="e.g. parking signature id or classifier verdict"
    )
    status_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    traffic_rank: Mapped[int | None] = mapped_column(
        Integer, nullable=True, index=True, comment="Tranco rank, feeds FR-SC-02"
    )
    ecommerce_confidence: Mapped[float | None] = mapped_column(
        Float, nullable=True, comment="FR-DS-08 classifier score 0..1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    hosts: Mapped[list[Host]] = relationship(back_populates="domain")
    sources: Mapped[list[DomainSource]] = relationship(back_populates="domain")


class Host(Base):
    """A hostname inside a domain (`shop.brand.com`), FR-DS-05."""

    __tablename__ = "host"
    __table_args__ = (UniqueConstraint("hostname", name="uq_host_hostname"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    domain_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("domain.id", ondelete="CASCADE"), nullable=False, index=True
    )
    hostname: Mapped[str] = mapped_column(String(253), nullable=False)
    is_primary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    last_resolved_ips: Mapped[list[str] | None] = mapped_column(ARRAY(INET), nullable=True)
    asn: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    hosting_country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    cname: Mapped[str | None] = mapped_column(String(253), nullable=True)
    mx: Mapped[list[str] | None] = mapped_column(ARRAY(String(253)), nullable=True)
    ns: Mapped[list[str] | None] = mapped_column(ARRAY(String(253)), nullable=True)
    dns_status: Mapped[str | None] = mapped_column(
        String(16), nullable=True, comment="ok | no_dns | error (FR-DS-06)"
    )
    dns_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    domain: Mapped[Domain] = relationship(back_populates="hosts")


class DomainSource(Base):
    """Lineage: where and when a domain was seen (FR-DS-03, AS-22)."""

    __tablename__ = "domain_source"
    __table_args__ = (
        UniqueConstraint("domain_id", "source", name="uq_domain_source_domain_id_source"),
        Index("ix_domain_source_source_last_seen", "source", "last_seen"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    domain_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("domain.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[DomainSourceKind] = mapped_column(
        enum_column(DomainSourceKind, name="domain_source_kind"), nullable=False
    )
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    batch_id: Mapped[str] = mapped_column(String(128), nullable=False)

    domain: Mapped[Domain] = relationship(back_populates="sources")


class ImportBatch(Base):
    """One import run of a discovery source; `domain_source.batch_id` points here (FR-DS-03)."""

    __tablename__ = "import_batch"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    source: Mapped[DomainSourceKind] = mapped_column(
        enum_column(DomainSourceKind, name="import_batch_source"), nullable=False, index=True
    )
    origin: Mapped[str] = mapped_column(
        Text, nullable=False, comment="file name, URL or feed identifier"
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    records_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_invalid: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    domains_new: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    domains_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    hosts_new: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
