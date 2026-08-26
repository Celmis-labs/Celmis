"""automation_runs.language — the language the question was asked in

Revision ID: e7b204c9f138
Revises: d5c1b8e4a730
Create Date: 2026-08-19

The canned parts of the agent's reply — what it is, what it can do — are
written out in sixteen languages precisely so that saying them costs no
model tokens. They were rendered in the INTERFACE language, so a question
asked in Ukrainian was answered with an English paragraph.

The model has already read the sentence; it reports the code as one extra
field on a reply that was being paid for anyway.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "e7b204c9f138"
down_revision = "d5c1b8e4a730"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("automation_runs", sa.Column("language", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("automation_runs", "language")
