"""Precomputed market-share cells (NFR-P-05, ADR-0033).

`GET /v1/stats/market-share` reads this snapshot instead of aggregating 1.5M
`store_provider` rows per request; `payintel stats refresh` rebuilds it.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from payintel.core.models.base import Base


class MarketShareCell(Base):
    """One row per (role, country, platform, provider) of C1-visible stores.

    `role` NULL = any role; `provider_id` NULL = the cell total (stores in the
    cell); `provider_id = 'other'` = distinct stores whose providers were
    below `min_cell` at refresh time (FR-RP-03). Segments and country /
    platform filters select whole cells, so one snapshot serves every client.
    """

    __tablename__ = "market_share_cell"
    __table_args__ = (
        Index("ix_market_share_cell_role_cell", "role", "country", "platform_id"),
        {
            "comment": "Snapshot read by GET /v1/stats/market-share; "
            "`payintel stats refresh` rebuilds it"
        },
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    role: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="NULL = any role")
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    platform_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="NULL = cell total; 'other' = providers below min_cell (FR-RP-03)",
    )
    stores: Mapped[int] = mapped_column(Integer, nullable=False)
    min_cell: Mapped[int] = mapped_column(Integer, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
