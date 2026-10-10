"""Debt sprint 3: manual review verdict on change events (NFR-Q-09).

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-11 00:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "change_event",
        sa.Column(
            "review_decision",
            sa.String(length=16),
            nullable=True,
            comment="NFR-Q-09: accepted / rejected by manual review",
        ),
    )
    op.add_column("change_event", sa.Column("reviewed_by", sa.String(length=128), nullable=True))
    op.add_column(
        "change_event", sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("change_event", "reviewed_at")
    op.drop_column("change_event", "reviewed_by")
    op.drop_column("change_event", "review_decision")
