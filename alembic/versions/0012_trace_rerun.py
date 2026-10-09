"""Debt after stage 4: manual re-run of a checkout walk with a Playwright trace (FR-QA-06).

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-09 22:30:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "scan_plan",
        sa.Column(
            "trace_requested",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="FR-QA-06: record a Playwright trace on the next checkout walk",
        ),
    )
    op.add_column(
        "scan_plan",
        sa.Column(
            "requested_by",
            sa.String(length=128),
            nullable=True,
            comment="Actor of the manual re-run request",
        ),
    )
    op.add_column(
        "scan_run",
        sa.Column(
            "trace_key",
            sa.String(length=512),
            nullable=True,
            comment="FR-QA-06: Playwright trace zip in the artefact bucket",
        ),
    )


def downgrade() -> None:
    op.drop_column("scan_run", "trace_key")
    op.drop_column("scan_plan", "requested_by")
    op.drop_column("scan_plan", "trace_requested")
