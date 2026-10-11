"""Debt sprint 4: worker heartbeats with host load for the admin crawler page (FR-ADM-03).

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-11 18:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "worker_heartbeat",
        sa.Column("worker_id", sa.String(length=128), primary_key=True),
        sa.Column("kind", sa.String(length=32), nullable=False, comment="light | checkout | …"),
        sa.Column("hostname", sa.String(length=255), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=False),
        sa.Column("load1", sa.Float(), nullable=False, comment="1-minute load average"),
        sa.Column("load5", sa.Float(), nullable=False, comment="5-minute load average"),
        sa.Column("cpu_count", sa.Integer(), nullable=False),
        sa.Column("mem_total_mb", sa.Integer(), nullable=False),
        sa.Column("mem_used_mb", sa.Integer(), nullable=False),
        sa.Column("concurrency", sa.Integer(), nullable=False),
        sa.Column("in_flight", sa.Integer(), nullable=False),
        sa.Column("scans_done", sa.BigInteger(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "stopped_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="set by the last beat of a clean shutdown",
        ),
        comment="FR-ADM-03: worker liveness and host load (core/heartbeat.py)",
    )


def downgrade() -> None:
    op.drop_table("worker_heartbeat")
