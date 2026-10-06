"""repo_index_state.indexed_branch: which branch the indexed revision is on

The MCP dev profile prints `branch@sha` and an age on every answer so an
agent knows what the line numbers refer to. The sha was already recorded; the
branch lived only in the git clone, which an answer cannot rely on (the clone
can be re-pointed, deleted or detached after the index was built). The branch
is written next to the sha by `record_index_success` and read back here.

Nullable, no default: rows written before this migration, and indexes whose
checkout named no branch, stay NULL and are shown as unknown.

Idempotent: a column that is already there is left alone, so a database that
was stamped past this revision, or created from the models, upgrades cleanly.

Downgrade drops the column (batch_alter_table, so SQLite rebuilds the table).

Revision ID: b3e9d27f5a40
Revises: a7c41e9b2d11
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "b3e9d27f5a40"
# The chain: c1f6b8d0e256 -> a7c41e9b2d10 -> a7c41e9b2d11 -> b3e9d27f5a40.
down_revision = "a7c41e9b2d11"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return False
    return column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    if _has_column("repo_index_state", "indexed_branch"):
        return
    op.add_column(
        "repo_index_state", sa.Column("indexed_branch", sa.Text(), nullable=True))


def downgrade() -> None:
    if not _has_column("repo_index_state", "indexed_branch"):
        return
    with op.batch_alter_table("repo_index_state") as batch:
        batch.drop_column("indexed_branch")
