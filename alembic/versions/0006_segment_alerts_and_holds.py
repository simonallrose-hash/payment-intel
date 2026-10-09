"""Stage 4: segment alert rules (FR-AL-05) and client deliveries held by a quality
anomaly (FR-QA-04).

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-09 16:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "alert_rule",
        sa.Column(
            "countries",
            postgresql.ARRAY(sa.String(2)),
            nullable=False,
            server_default="{}",
            comment="Segment rules only: ISO-3166 alpha-2 filter, empty = whole segment",
        ),
    )
    op.add_column(
        "alert_rule",
        sa.Column(
            "platforms",
            postgresql.ARRAY(sa.String(64)),
            nullable=False,
            server_default="{}",
            comment="Segment rules only: platform_id filter, empty = whole segment",
        ),
    )
    op.add_column(
        "delivery",
        sa.Column(
            "held_by_alert_id",
            sa.BigInteger(),
            sa.ForeignKey("quality_alert.id", ondelete="SET NULL"),
            nullable=True,
            comment="FR-QA-04: delivery waits for this anomaly alert to be confirmed",
        ),
    )
    op.create_index(
        "ix_delivery_held_by_alert_id",
        "delivery",
        ["held_by_alert_id"],
        postgresql_where=sa.text("held_by_alert_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_delivery_held_by_alert_id", table_name="delivery")
    op.drop_column("delivery", "held_by_alert_id")
    op.drop_column("alert_rule", "platforms")
    op.drop_column("alert_rule", "countries")
