"""completed_comment and commands_guide_enabled: the Kodus-style closing comment

Two more inheritable review settings, at BOTH layers (`repo_review_policies`
and `workspace_review_defaults`), NULL = inherit, no server default:

1. `completed_comment` (text) — `completed` (the built-in: "Code Review
   Completed", with the findings, the scope and, when it is on, the guide of
   commands) or `classic` (the summary layout of earlier releases).
2. `commands_guide_enabled` (boolean) — whether the completed comment lists
   the bot's commands. Off by default until the commands exist.

Idempotent: a column that is already there is left alone, so a database that
was stamped past this revision, or created from the models, upgrades cleanly.

Downgrade drops both columns from both tables (batch_alter_table, so SQLite
rebuilds the table).

Revision ID: d6a1c3e5f701
Revises: c5d6e7f8a9b0
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "d6a1c3e5f701"
down_revision = "c5d6e7f8a9b0"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(table):
        return False
    return column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    # The table names are spelled inline so the migration-chain test can bind
    # the loop variable to its literal names.
    for table in ("repo_review_policies", "workspace_review_defaults"):
        if not _has_column(table, "completed_comment"):
            op.add_column(table, sa.Column("completed_comment", sa.Text(), nullable=True))
        if not _has_column(table, "commands_guide_enabled"):
            op.add_column(
                table, sa.Column("commands_guide_enabled", sa.Boolean(), nullable=True))


def downgrade() -> None:
    for table in ("repo_review_policies", "workspace_review_defaults"):
        present = [
            c for c in ("completed_comment", "commands_guide_enabled")
            if _has_column(table, c)
        ]
        if not present:
            continue
        with op.batch_alter_table(table) as batch:
            for column in present:
                batch.drop_column(column)
