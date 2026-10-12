"""Debt sprint 6: third-party script URLs fetched once by the script pool (FR-LS-03, ADR-0038).

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-12 22:30:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "js_asset_url",
        sa.Column("url", sa.String(length=2048), primary_key=True),
        sa.Column("hostname", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, comment="pending | ok | error"),
        sa.Column(
            "sha256",
            sa.String(length=64),
            sa.ForeignKey("js_asset.sha256", ondelete="SET NULL"),
            nullable=True,
            comment="body hash once fetched (kept while a refresh is pending)",
        ),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(length=300), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "next_fetch_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="pending: not before; ok: refresh after light.third_party_js_ttl_days",
        ),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.String(length=128), nullable=True),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        comment="FR-LS-03: third-party scripts are downloaded once, by URL (ADR-0038)",
    )
    op.create_index("ix_js_asset_url_hostname", "js_asset_url", ["hostname"])
    op.create_index("ix_js_asset_url_status", "js_asset_url", ["status"])
    op.create_table(
        "js_asset_url_host",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "url",
            sa.String(length=2048),
            sa.ForeignKey("js_asset_url.url", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "host_id", sa.BigInteger(), sa.ForeignKey("host.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("url", "host_id", name="uq_js_asset_url_host"),
        comment="hosts waiting for a script URL; becomes host_js_asset when fetched",
    )
    op.create_index("ix_js_asset_url_host_host_id", "js_asset_url_host", ["host_id"])


def downgrade() -> None:
    op.drop_table("js_asset_url_host")
    op.drop_index("ix_js_asset_url_status", table_name="js_asset_url")
    op.drop_index("ix_js_asset_url_hostname", table_name="js_asset_url")
    op.drop_table("js_asset_url")
