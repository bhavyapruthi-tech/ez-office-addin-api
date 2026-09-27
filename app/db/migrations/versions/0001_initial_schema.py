"""initial schema

Revision ID: ca0ca6ee4c59
Revises:
Create Date: 2026-09-25 12:58:52.046454

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "ca0ca6ee4c59"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("work_email", sa.String(), nullable=False),
        sa.Column("ms_oid", sa.String(), nullable=False),
        sa.Column("ez_workspace_account_id", sa.String(), nullable=True),
        sa.Column("ez_wallet_id", sa.String(), nullable=True),
        sa.Column(
            "workspace_account_status",
            sa.String(),
            nullable=False,
            server_default="unprovisioned",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("work_email", name="uq_users_work_email"),
        sa.UniqueConstraint("ms_oid", name="uq_users_ms_oid"),
    )
    op.create_index("ix_users_work_email", "users", ["work_email"])
    op.create_index("ix_users_ms_oid", "users", ["ms_oid"])

    op.create_table(
        "sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        # KD7/R3: nullable, no logout endpoint sets it yet.
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("token_hash", name="uq_sessions_token_hash"),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_index("ix_sessions_token_hash", "sessions", ["token_hash"])

    op.create_table(
        "tool_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("operation", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="queued"),
        sa.Column("failed_operation", sa.String(), nullable=True),
        # R11/KD8: insufficient_balance_after_spend | workspace_unreachable |
        # stale_timeout | debit_succeeded_status_write_failed
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("input_file_meta", sa.JSON(), nullable=True),
        sa.Column("result_file_url", sa.String(), nullable=True),
        sa.Column(
            "expert_review_requested",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        # R19: persisted once generated so repeated polls return the same value.
        sa.Column("localization_ref", sa.String(), nullable=True),
        sa.Column("credits_cost", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "user_id", "idempotency_key", name="uq_tool_jobs_user_idem"
        ),
    )
    op.create_index("ix_tool_jobs_user_id", "tool_jobs", ["user_id"])
    op.create_index("ix_tool_jobs_status", "tool_jobs", ["status"])
    op.create_index("ix_tool_jobs_created_at", "tool_jobs", ["created_at"])

    op.create_table(
        "briefs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("division", sa.String(), nullable=False),
        sa.Column("capability", sa.String(), nullable=True),
        sa.Column("output_format", sa.String(), nullable=True),
        sa.Column("deadline", sa.String(), nullable=True),
        sa.Column("notes", sa.String(), nullable=True),
        sa.Column("upsells", sa.JSON(), nullable=True),
        sa.Column("ref_number", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("ref_number", name="uq_briefs_ref_number"),
    )
    op.create_index("ix_briefs_user_id", "briefs", ["user_id"])
    op.create_index("ix_briefs_ref_number", "briefs", ["ref_number"])

    op.create_table(
        "stripe_webhook_events",
        sa.Column("event_id", sa.String(), primary_key=True),
        sa.Column("credited", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("stripe_webhook_events")
    op.drop_index("ix_briefs_ref_number", table_name="briefs")
    op.drop_index("ix_briefs_user_id", table_name="briefs")
    op.drop_table("briefs")
    op.drop_index("ix_tool_jobs_created_at", table_name="tool_jobs")
    op.drop_index("ix_tool_jobs_status", table_name="tool_jobs")
    op.drop_index("ix_tool_jobs_user_id", table_name="tool_jobs")
    op.drop_table("tool_jobs")
    op.drop_index("ix_sessions_token_hash", table_name="sessions")
    op.drop_index("ix_sessions_user_id", table_name="sessions")
    op.drop_table("sessions")
    op.drop_index("ix_users_ms_oid", table_name="users")
    op.drop_index("ix_users_work_email", table_name="users")
    op.drop_table("users")
