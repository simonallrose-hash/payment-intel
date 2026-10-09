"""Stage 4: re-KYC schedule (FR-KYC-06) and OpenSanctions screening details (FR-KYC-03).

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-09 19:30:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "kyc_record",
        sa.Column(
            "next_review_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="FR-KYC-06: re-KYC due date (12 months after approval, at once on "
            "beneficiary change)",
        ),
    )
    op.add_column(
        "kyc_record",
        sa.Column(
            "reminder_sent_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="FR-KYC-06: when the 30-day reminder for the current due date went out",
        ),
    )
    op.add_column(
        "kyc_record",
        sa.Column(
            "sanctions_details",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="FR-KYC-03: OpenSanctions hits per query (id, caption, score, datasets)",
        ),
    )
    op.create_index(
        "ix_kyc_record_next_review_at",
        "kyc_record",
        ["next_review_at"],
        postgresql_where=sa.text("next_review_at IS NOT NULL"),
    )
    # organisations approved before this migration keep the 12-month cycle from their decision
    op.execute(
        "UPDATE kyc_record SET next_review_at = decided_at + interval '365 days' "
        "WHERE decision = 'approved' AND decided_at IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_index("ix_kyc_record_next_review_at", table_name="kyc_record")
    op.drop_column("kyc_record", "sanctions_details")
    op.drop_column("kyc_record", "reminder_sent_at")
    op.drop_column("kyc_record", "next_review_at")
