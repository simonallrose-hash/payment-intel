"""Stage 3: report jobs can be addressed to a client organisation (portal "Reports" page).

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-09 14:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "report_job",
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organization.id", ondelete="SET NULL"),
            nullable=True,
            comment="Recipient organisation; NULL for internal reports (FR-RP-01)",
        ),
    )
    op.create_index("ix_report_job_org_id", "report_job", ["org_id"])


def downgrade() -> None:
    op.drop_index("ix_report_job_org_id", table_name="report_job")
    op.drop_column("report_job", "org_id")
