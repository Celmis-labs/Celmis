"""automation_runs: a plan is a list of steps, and the reading is a job

Revision ID: f2a8c05d7e14
Revises: e1c7d4a90b35
Create Date: 2026-08-18

One sentence is often two jobs — arm review on a release branch and, in the
same breath, audit a feature branch. A single action column could only record
whichever half the model happened to pick.

`job_id` is what makes the reading stoppable: it runs on the queue rather than
inside the request, so navigating away no longer discards it.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "f2a8c05d7e14"
down_revision = "e1c7d4a90b35"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("automation_runs",
                  sa.Column("steps", JSONB(), nullable=False, server_default="[]"))
    op.add_column("automation_runs", sa.Column("job_id", sa.Text(), nullable=True))
    # Rows written before this migration carry a single action. Folded into
    # the new shape so the history does not have two formats in it.
    op.execute("""
        UPDATE automation_runs
           SET steps = jsonb_build_array(jsonb_build_object(
                 'action', action,
                 'arguments', arguments,
                 'note', note,
                 'resolved_repos', resolved_repos,
                 'blocked', blocked))
         WHERE action IS NOT NULL AND steps = '[]'::jsonb
    """)


def downgrade() -> None:
    op.drop_column("automation_runs", "job_id")
    op.drop_column("automation_runs", "steps")
