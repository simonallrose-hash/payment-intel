"""Stage 2: current third-party checkout hosts (FR-DT-11, FR-HI-02) and quality alerts (FR-QA-06).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-08 23:40:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "store_checkout_host",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("host_id", sa.BigInteger(), nullable=False),
        sa.Column("third_party_etld1", sa.String(length=253), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("provider_id", sa.String(length=64), nullable=True),
        sa.Column("request_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("first_seen", sa.Date(), nullable=False),
        sa.Column("last_seen", sa.Date(), nullable=False),
        sa.Column("confirmations", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("misses", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.ForeignKeyConstraint(
            ["host_id"],
            ["host.id"],
            name=op.f("fk_store_checkout_host_host_id_host"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["provider.id"],
            name=op.f("fk_store_checkout_host_provider_id_provider"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_store_checkout_host")),
        sa.UniqueConstraint(
            "host_id", "third_party_etld1", name="uq_store_checkout_host_host_id_etld1"
        ),
    )
    op.create_index(
        "ix_store_checkout_host_third_party_etld1",
        "store_checkout_host",
        ["third_party_etld1"],
        unique=False,
    )
    op.create_table(
        "quality_alert",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("subject", sa.String(length=128), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("baseline", sa.Float(), nullable=True),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("window_days", sa.Integer(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_by", sa.String(length=128), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_quality_alert")),
    )
    op.create_index("ix_quality_alert_detected_at", "quality_alert", ["detected_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_quality_alert_detected_at", table_name="quality_alert")
    op.drop_table("quality_alert")
    op.drop_index("ix_store_checkout_host_third_party_etld1", table_name="store_checkout_host")
    op.drop_table("store_checkout_host")
