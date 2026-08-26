"""Embedded Claude Code agent sessions + append-only event log.

Revision ID: c8e4f2a91d37
Revises: b7f4c2e908a1
Create Date: 2026-08-06
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "c8e4f2a91d37"
down_revision = "b7f4c2e908a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_sessions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("repo_slug", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("result", JSONB(), nullable=False, server_default="{}"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("runner_instance", sa.Text(), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_agent_sessions_ws", "agent_sessions", ["workspace_id", "created_at"])
    op.create_index("ix_agent_sessions_user", "agent_sessions", ["user_id"])

    op.create_table(
        "agent_session_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("data", JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_agent_events_session", "agent_session_events", ["session_id", "id"])


def downgrade() -> None:
    op.drop_index("ix_agent_events_session", table_name="agent_session_events")
    op.drop_table("agent_session_events")
    op.drop_index("ix_agent_sessions_user", table_name="agent_sessions")
    op.drop_index("ix_agent_sessions_ws", table_name="agent_sessions")
    op.drop_table("agent_sessions")
