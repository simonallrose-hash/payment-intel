"""Debt after stage 4: precomputed market-share cells for NFR-P-05 (ADR-0033).

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-09 23:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "market_share_cell",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("role", sa.String(length=32), nullable=True, comment="NULL = any role"),
        sa.Column("country", sa.String(length=2), nullable=True),
        sa.Column("platform_id", sa.String(length=64), nullable=True),
        sa.Column(
            "provider_id",
            sa.String(length=64),
            nullable=True,
            comment="NULL = cell total; 'other' = providers below min_cell (FR-RP-03)",
        ),
        sa.Column("stores", sa.Integer(), nullable=False),
        sa.Column("min_cell", sa.Integer(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_market_share_cell"),
        comment="Snapshot read by GET /v1/stats/market-share; `payintel stats refresh` rebuilds it",
    )
    op.create_index(
        "ix_market_share_cell_role_cell", "market_share_cell", ["role", "country", "platform_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_market_share_cell_role_cell", table_name="market_share_cell")
    op.drop_table("market_share_cell")
