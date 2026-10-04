"""repo policy: summary, started comment, language and inline-comment cap

Five per-repository review-output settings on `repo_review_policies`:

- `summary_enabled` (bool, default TRUE) and `summary_instructions` (text) —
  whether the PR summary is posted, and extra guidance for writing it;
- `started_comment_enabled` (bool, default TRUE) — the "review started" note;
- `review_language` (text) — output language code; NULL = workspace default;
- `max_inline_comments` (int) — inline-comment cap; NULL = REVIEW_MAX_INLINE_COMMENTS.

All nullable. The two switches carry a server default of TRUE so every policy
row that predates them keeps posting what it already posted.

Literal `op.add_column` calls only — see tests/db/test_migration_chain.py.

Revision ID: d7a3e9c51b64
Revises: f6b1d3a8c240
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "d7a3e9c51b64"
down_revision = "f6b1d3a8c240"
branch_labels = None
depends_on = None

_TABLE = "repo_review_policies"


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    if not _has_column(_TABLE, "summary_enabled"):
        op.add_column(
            "repo_review_policies",
            sa.Column("summary_enabled", sa.Boolean(), nullable=True,
                      server_default=sa.true()),
        )
    if not _has_column(_TABLE, "summary_instructions"):
        op.add_column(
            "repo_review_policies",
            sa.Column("summary_instructions", sa.Text(), nullable=True),
        )
    if not _has_column(_TABLE, "started_comment_enabled"):
        op.add_column(
            "repo_review_policies",
            sa.Column("started_comment_enabled", sa.Boolean(), nullable=True,
                      server_default=sa.true()),
        )
    if not _has_column(_TABLE, "review_language"):
        op.add_column(
            "repo_review_policies",
            sa.Column("review_language", sa.Text(), nullable=True),
        )
    if not _has_column(_TABLE, "max_inline_comments"):
        op.add_column(
            "repo_review_policies",
            sa.Column("max_inline_comments", sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    for column in (
        "max_inline_comments",
        "review_language",
        "started_comment_enabled",
        "summary_instructions",
        "summary_enabled",
    ):
        if _has_column(_TABLE, column):
            op.drop_column(_TABLE, column)
