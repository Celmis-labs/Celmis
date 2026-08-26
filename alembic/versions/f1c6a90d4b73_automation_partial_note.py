"""automation_runs.partial_note — the sentence while it is still being written

Revision ID: f1c6a90d4b73
Revises: e7b204c9f138
Create Date: 2026-08-19

Reading a sentence into a plan takes 2-6 seconds on production, and until now
none of it was visible: the row went from "reading" to a finished plan in one
write, so a person watched a spinner for the whole of it and reported, exactly
right, that "the whole answer is generated and then shown".

The planner streams now, and the model is asked to write the human-readable
`note` BEFORE the machine-readable steps, so the sentence is complete about a
second in. This column is where it lands while the rest is still being
generated — throttled to a few writes a second, not one per token.

A column rather than a socket, because persisting it is the part that beats
ordinary streaming: close the page mid-sentence and the sentence is still
there when you come back.

Nullable, no backfill: every existing row finished before this shipped and has
nothing partial to remember.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "f1c6a90d4b73"
down_revision = "e7b204c9f138"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "automation_runs",
        sa.Column("partial_note", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("automation_runs", "partial_note")
