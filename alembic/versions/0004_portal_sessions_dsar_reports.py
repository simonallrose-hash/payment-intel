"""Stage 3: portal sessions, GDPR requests, report jobs, dispatcher cursor, 2FA enrolment.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-09 10:30:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "user_account",
        sa.Column(
            "totp_confirmed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="2FA enrolment completed (FR-UI-01)",
        ),
    )
    op.create_table(
        "portal_session",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("csrf_token", sa.String(length=64), nullable=False),
        sa.Column("totp_verified", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user_account.id"],
            name=op.f("fk_portal_session_user_id_user_account"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_portal_session")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_portal_session_token_hash")),
    )
    op.create_table(
        "dsar_request",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "kind",
            sa.Enum("access", "erasure", name="dsar_kind", native_enum=False, length=48),
            nullable=False,
        ),
        sa.Column("subject", sa.String(length=256), nullable=False),
        sa.Column("contact", sa.String(length=254), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "open", "in_progress", "closed", name="dsar_status", native_enum=False, length=48
            ),
            server_default="open",
            nullable=False,
        ),
        sa.Column("handled_by", sa.String(length=128), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dsar_request")),
    )
    op.create_table(
        "report_job",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("spec", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending", "done", "failed", name="report_status", native_enum=False, length=48
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("requested_by", sa.String(length=128), nullable=False),
        sa.Column("xlsx_key", sa.String(length=512), nullable=True),
        sa.Column("csv_key", sa.String(length=512), nullable=True),
        sa.Column(
            "summary",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_report_job")),
    )
    op.create_table(
        "system_cursor",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_system_cursor")),
    )
    # One delivery per (rule, event): the dispatcher is idempotent across restarts.
    op.create_index(
        "uq_delivery_rule_event",
        "delivery",
        ["alert_rule_id", "change_event_id"],
        unique=True,
        postgresql_where=sa.text("change_event_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_delivery_rule_event", table_name="delivery")
    op.drop_table("system_cursor")
    op.drop_table("report_job")
    op.drop_table("dsar_request")
    op.drop_table("portal_session")
    op.drop_column("user_account", "totp_confirmed_at")
