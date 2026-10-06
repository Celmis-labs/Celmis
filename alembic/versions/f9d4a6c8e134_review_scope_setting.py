"""the review scope setting

One more inheritable review setting, at BOTH layers (`repo_review_policies`
and `workspace_review_defaults`), NULL = inherit, no server default:

* `review_scope` (text) — `incremental` (the built-in: a push is reviewed from
  the last reviewed commit on, whenever that is safe) or `full` (every review
  reads the whole pull request).

Idempotent: a column that is already there is left alone, so a database that
was stamped past this revision, or created from the models, upgrades cleanly.

Downgrade drops the column (batch_alter_table, so SQLite rebuilds the table).

Revision ID: f9d4a6c8e134
Revises: e7b2d4f6a812
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "f9d4a6c8e134"
down_revision = "e7b2d4f6a812"
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
        if not _has_column(table, "review_scope"):
            op.add_column(table, sa.Column("review_scope", sa.Text(), nullable=True))


def downgrade() -> None:
    for table in ("repo_review_policies", "workspace_review_defaults"):
        if _has_column(table, "review_scope"):
            with op.batch_alter_table(table) as batch:
                batch.drop_column("review_scope")
