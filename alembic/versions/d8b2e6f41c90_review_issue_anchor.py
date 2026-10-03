"""review_issues.anchor — the flagged line, so a fix is read from the code

The first live run of "fixed in a subsequent commit" called a divide-by-zero
fixed that nobody had touched: the next commit changed another function in the
same file (so the file's diff section hash moved) and the model, this time, did
not repeat the finding. A file changing is not the flagged line changing.

`anchor` holds the text of the line the finding pointed at, read from the
reviewed diff. An unrepeated issue whose anchor is still in the file's new diff
stays open.

Literal `op.add_column` only — see tests/db/test_migration_chain.py.

Revision ID: d8b2e6f41c90
Revises: c4f19a7d2e83
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "d8b2e6f41c90"
down_revision = "c4f19a7d2e83"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    if not _has_column("review_issues", "anchor"):
        op.add_column("review_issues", sa.Column("anchor", sa.Text(), nullable=True))


def downgrade() -> None:
    if _has_column("review_issues", "anchor"):
        op.drop_column("review_issues", "anchor")
