"""Stage 4: usage anomaly incidents (FR-AB-02/03), canary hits (FR-AB-04),
quarterly usage reports (FR-AB-05) and organisation restriction.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-09 18:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "organization",
        sa.Column(
            "restricted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="FR-AB-03: API keys refused until compliance lifts the restriction",
        ),
    )
    op.add_column(
        "organization", sa.Column("restricted_reason", sa.String(256), nullable=True)
    )
    op.create_table(
        "abuse_incident",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organization.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("detector", sa.String(48), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False, comment="low|medium|high|critical"),
        sa.Column("summary", sa.String(512), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, comment="open|resolved|dismissed"),
        sa.Column(
            "auto_restricted", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.String(128), nullable=True),
        sa.Column("resolution", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_abuse_incident"),
    )
    op.create_index("ix_abuse_incident_org_id", "abuse_incident", ["org_id"])
    op.create_index("ix_abuse_incident_status", "abuse_incident", ["status"])
    op.create_table(
        "canary_hit",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "canary_id",
            sa.BigInteger(),
            sa.ForeignKey("canary.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organization.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False, comment="dns|http|email"),
        sa.Column("source", sa.String(256), nullable=True),
        sa.Column("detail", sa.String(512), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_canary_hit"),
    )
    op.create_index("ix_canary_hit_org_id", "canary_hit", ["org_id"])
    op.create_index("ix_canary_hit_observed_at", "canary_hit", ["observed_at"])
    op.create_table(
        "usage_report",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organization.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("period", sa.String(8), nullable=False, comment="e.g. 2026-Q3"),
        sa.Column("file_key", sa.String(512), nullable=True),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_usage_report"),
        sa.UniqueConstraint("org_id", "period", name="uq_usage_report_org_period"),
    )


def downgrade() -> None:
    op.drop_table("usage_report")
    op.drop_index("ix_canary_hit_observed_at", table_name="canary_hit")
    op.drop_index("ix_canary_hit_org_id", table_name="canary_hit")
    op.drop_table("canary_hit")
    op.drop_index("ix_abuse_incident_status", table_name="abuse_incident")
    op.drop_index("ix_abuse_incident_org_id", table_name="abuse_incident")
    op.drop_table("abuse_incident")
    op.drop_column("organization", "restricted_reason")
    op.drop_column("organization", "restricted_at")
