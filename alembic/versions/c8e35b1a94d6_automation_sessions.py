"""automation_runs.session_id — group the asks into conversations

Revision ID: c8e35b1a94d6
Revises: a4d1f8b62c07
Create Date: 2026-08-18

The agent has no multi-turn memory: every sentence is read on its own, and
that is deliberate. A session is therefore a grouping for READING BACK rather
than a context window — "the four things I asked on Tuesday while setting up
the release" is one thread to a person and four unrelated rows to a database.

Existing rows are backfilled one session per calendar day, which is the
closest honest reconstruction: nothing recorded which browser visit they came
from, and inventing a session per row would be worse than a rough grouping.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "c8e35b1a94d6"
down_revision = "a4d1f8b62c07"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("automation_runs", sa.Column("session_id", sa.Text(), nullable=True))
    op.create_index("ix_automation_runs_session", "automation_runs",
                    ["workspace_id", "session_id"])
    op.execute("""
        UPDATE automation_runs
           SET session_id = 'day-' || to_char(created_at, 'YYYY-MM-DD')
         WHERE session_id IS NULL
    """)


def downgrade() -> None:
    op.drop_index("ix_automation_runs_session", table_name="automation_runs")
    op.drop_column("automation_runs", "session_id")
