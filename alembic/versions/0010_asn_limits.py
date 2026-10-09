"""Stage 4: ASN rate limits after hoster complaints (FR-OO-04).

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-09 20:30:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "asn_limit",
        sa.Column("asn", sa.BigInteger(), nullable=False),
        sa.Column(
            "factor",
            sa.Float(),
            nullable=False,
            comment="Multiplier on per-host and per-IP request rates (0 < factor ≤ 1)",
        ),
        sa.Column(
            "asn_rps",
            sa.Float(),
            nullable=False,
            comment="Cap on requests per second across the whole ASN while limited",
        ),
        sa.Column("source", sa.String(length=256), nullable=False, comment="abuse@…, ticket id"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("ip", sa.String(length=64), nullable=True, comment="address named in the complaint"),
        sa.Column("asn_name", sa.String(length=256), nullable=True),
        sa.Column("complaints", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("last_complaint_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "lifted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Set by staff after the manual review; the limit no longer applies",
        ),
        sa.Column("lifted_by", sa.String(length=128), nullable=True),
        sa.PrimaryKeyConstraint("asn", name="pk_asn_limit"),
        comment="FR-OO-04: reduced crawl rates for an ASN after a hoster complaint",
    )


def downgrade() -> None:
    op.drop_table("asn_limit")
