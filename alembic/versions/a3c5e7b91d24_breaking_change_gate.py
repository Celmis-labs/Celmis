"""the breaking-change gate setting

One more inheritable review setting, at BOTH layers (`repo_review_policies`
and `workspace_review_defaults`), NULL = inherit, no server default:

* `breaking_change_gate` (boolean) - post the `celmis/breaking-change` commit
  status on the pull request head. The built-in is off.

Idempotent: a column that is already there is left alone, so a database that
was stamped past this revision, or created from the models, upgrades cleanly.

Downgrade drops the column (batch_alter_table, so SQLite rebuilds the table).

Revision ID: a3c5e7b91d24
Revises: e2b7c4d9a1f3
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "a3c5e7b91d24"
down_revision = "e2b7c4d9a1f3"
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
        if not _has_column(table, "breaking_change_gate"):
            op.add_column(table, sa.Column("breaking_change_gate", sa.Boolean(), nullable=True))


def downgrade() -> None:
    for table in ("repo_review_policies", "workspace_review_defaults"):
        if _has_column(table, "breaking_change_gate"):
            with op.batch_alter_table(table) as batch:
                batch.drop_column("breaking_change_gate")
