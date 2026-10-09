"""Stage 4: PDF reports (FR-RP-04) and the public summary (FR-RP-05).

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-09 21:30:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("report_job", sa.Column("pdf_key", sa.String(length=512), nullable=True))
    op.add_column(
        "report_job",
        sa.Column(
            "public_summary",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="FR-RP-05: aggregates without domains, built with the report",
        ),
    )
    op.add_column(
        "report_job",
        sa.Column(
            "public_slug",
            sa.String(length=96),
            nullable=True,
            comment="FR-RP-05: path of the public page while published",
        ),
    )
    op.add_column("report_job", sa.Column("public_pdf_key", sa.String(length=512), nullable=True))
    op.add_column(
        "report_job", sa.Column("published_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("report_job", sa.Column("published_by", sa.String(length=128), nullable=True))
    op.create_index("uq_report_job_public_slug", "report_job", ["public_slug"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_report_job_public_slug", table_name="report_job")
    for col in (
        "published_by",
        "published_at",
        "public_pdf_key",
        "public_slug",
        "public_summary",
        "pdf_key",
    ):
        op.drop_column("report_job", col)
