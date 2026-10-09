"""Stage 4: manual review of findings (FR-QA-05) — decisions table and the
`suppressed` flag that hides rejected findings from clients.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-09 17:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("store_provider", "store_payment_method"):
        op.add_column(
            table,
            sa.Column(
                "suppressed",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
                comment="FR-QA-05: rejected by an analyst, hidden from clients",
            ),
        )
    op.create_table(
        "finding_review",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "host_id",
            sa.BigInteger(),
            sa.ForeignKey("host.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("entity_type", sa.String(32), nullable=False),
        sa.Column("entity_id", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False, comment="confirmed | rejected"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="Snapshot of the observations shown to the analyst",
        ),
        sa.Column("reviewed_by", sa.String(128), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_finding_review"),
        sa.UniqueConstraint(
            "host_id", "entity_type", "entity_id", name="uq_finding_review_host_entity"
        ),
    )
    op.create_index("ix_finding_review_reviewed_at", "finding_review", ["reviewed_at"])


def downgrade() -> None:
    op.drop_index("ix_finding_review_reviewed_at", table_name="finding_review")
    op.drop_table("finding_review")
    for table in ("store_payment_method", "store_provider"):
        op.drop_column(table, "suppressed")
