"""agent sessions: execution mode + model choice

Two knobs the user picks when starting a session: how it runs (one agent, or
subagents fanning out in parallel) and which model runs it. Both are recorded
on the row rather than resolved at start time, so a session's transcript can
still be read back with the settings it actually ran under.

Revision ID: d7a1c93e5f28
Revises: e1c8b7a45d92
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d7a1c93e5f28"
down_revision: str | Sequence[str] | None = "e1c8b7a45d92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # server_default, not just default: existing rows predate the column and
    # the runner reads it on every session, including old ones being replayed.
    op.add_column(
        "agent_sessions",
        sa.Column("mode", sa.Text(), nullable=False, server_default="standard"),
    )
    op.add_column(
        "agent_sessions",
        sa.Column("model", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("agent_sessions", "model")
    op.drop_column("agent_sessions", "mode")
