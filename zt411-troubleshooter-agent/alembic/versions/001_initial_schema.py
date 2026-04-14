"""initial schema

Revision ID: 001
Revises:
Create Date: 2026-04-03
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("session_id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("os_platform", sa.String(16), nullable=False, server_default="unknown"),
        sa.Column("symptoms", JSONB, nullable=False, server_default="[]"),
        sa.Column("user_description", sa.Text, nullable=False, server_default=""),
        sa.Column("loop_status", sa.String(16), nullable=False, server_default="running"),
        sa.Column("loop_counter", sa.Integer, nullable=False, server_default="0"),
        sa.Column("escalation_reason", sa.Text, nullable=False, server_default=""),
        sa.Column("is_resolved", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("state_json", JSONB, nullable=False),
    )
    op.create_index("ix_sessions_loop_status", "sessions", ["loop_status"])
    op.create_index("ix_sessions_created_at", "sessions", ["created_at"])
    op.create_index("ix_sessions_is_resolved", "sessions", ["is_resolved"])


def downgrade() -> None:
    op.drop_table("sessions")
