"""Resumable agent sessions: transcript storage + paused state.

A session was an asyncio task holding a CLI subprocess, with the CLI's
transcript under CLAUDE_CONFIG_DIR inside the container. Both die on every
deploy, which made "come back tomorrow and carry on" impossible — the coming
back was the part that did not exist.

This adds the durable half:

  * `agent_session_transcripts` — the CLI's own conversation entries, mirrored
    out of the container through ClaudeAgentOptions.session_store and handed
    back verbatim on resume. Distinct from `agent_session_events`, which is
    what a human reads; neither substitutes for the other.
  * `resume_count` and `resumable_until` on the session, so a resumed
    conversation is legible as one and so the transcripts have an end date.
    They are the largest thing stored per session.

`paused` needs no migration — `status` is Text and the value is new.

Revision ID: d4a7f1c93e60
Revises: a2e91d40c7f5
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "d4a7f1c93e60"
down_revision = "a2e91d40c7f5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_session_transcripts",
        # BIGINT identity: this column IS the replay order. A timestamp ties
        # on a fast batch and a reordered conversation is not a conversation.
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("session_id", sa.Text(), nullable=False),
        # Subagent transcripts arrive under their own subpath; empty for main.
        sa.Column("subpath", sa.Text(), nullable=False, server_default=""),
        sa.Column("entry", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )
    # Every read is "this session, this subpath, in order".
    op.create_index(
        "ix_agent_transcripts_session", "agent_session_transcripts",
        ["session_id", "subpath", "id"],
    )
    # Retention sweeps by age across all sessions.
    op.create_index(
        "ix_agent_transcripts_created", "agent_session_transcripts",
        ["created_at"],
    )

    op.add_column(
        "agent_sessions",
        sa.Column("resume_count", sa.Integer(), nullable=False,
                  server_default="0"),
    )
    op.add_column(
        "agent_sessions",
        sa.Column("resumable_until", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_sessions", "resumable_until")
    op.drop_column("agent_sessions", "resume_count")
    op.drop_index("ix_agent_transcripts_created",
                  table_name="agent_session_transcripts")
    op.drop_index("ix_agent_transcripts_session",
                  table_name="agent_session_transcripts")
    op.drop_table("agent_session_transcripts")
