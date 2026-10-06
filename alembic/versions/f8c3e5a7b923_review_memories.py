"""review memories: team knowledge the reviewers are told, and its three settings

1. `review_memories` — one row per memory. `repo_slug` NULL is a memory for
   the whole workspace; a slug scopes it to that repository, and a `path_glob`
   narrows it to the files under it. `status` is active | pending | rejected:
   only active memories reach a prompt; what a machine proposed, or a person
   with no standing asked for, arrives pending. `origin` says how it came
   (manual | command | reply | agent | ui) and the `source_*` columns name the
   comment that asked for it.

2. `memories_enabled`, `knowledge_approval`, `memory_trusted_commenters` on
   `repo_review_policies` AND `workspace_review_defaults` — nullable, no
   server default, NULL = inherit (built-ins: on, on, none), like the
   2.3.0 settings.

Revision ID: f8c3e5a7b923
Revises: a9d4f6b8c034
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "f8c3e5a7b923"
down_revision = "a9d4f6b8c034"
branch_labels = None
depends_on = None

_MEMORIES = "review_memories"
_POLICY = "repo_review_policies"
_DEFAULTS = "workspace_review_defaults"


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return False
    return column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    if not _has_table(_MEMORIES):
        op.create_table(
            "review_memories",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("workspace_id", sa.Text(), nullable=False),
            sa.Column("repo_slug", sa.Text(), nullable=True),
            sa.Column("path_glob", sa.Text(), nullable=True),
            sa.Column("text", sa.Text(), nullable=False),
            sa.Column("status", sa.Text(), nullable=False,
                      server_default="pending"),
            sa.Column("origin", sa.Text(), nullable=False,
                      server_default="manual"),
            sa.Column("source_provider", sa.Text(), nullable=True),
            sa.Column("source_repo", sa.Text(), nullable=True),
            sa.Column("source_pr", sa.Integer(), nullable=True),
            sa.Column("source_comment_id", sa.Text(), nullable=True),
            sa.Column("source_url", sa.Text(), nullable=True),
            sa.Column("created_by", sa.Text(), nullable=True),
            sa.Column("updated_by", sa.Text(), nullable=True),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
        )
        op.create_index("ix_review_memories_scope", "review_memories",
                        ["workspace_id", "repo_slug", "status"])

    # The tuples are spelled inline (not module constants) so the
    # migration-chain test can bind the loop variable to its literal names.
    for table in ("repo_review_policies", "workspace_review_defaults"):
        for bool_column in ("memories_enabled", "knowledge_approval"):
            if not _has_column(table, bool_column):
                op.add_column(table, sa.Column(bool_column, sa.Boolean(), nullable=True))
        if not _has_column(table, "memory_trusted_commenters"):
            op.add_column(table, sa.Column(
                "memory_trusted_commenters", postgresql.JSONB(astext_type=sa.Text()),
                nullable=True))


def downgrade() -> None:
    # batch_alter_table so SQLite rebuilds the table; on Postgres it is a plain
    # ALTER TABLE … DROP COLUMN per column.
    for table in (_POLICY, _DEFAULTS):
        present = [
            c for c in ("memories_enabled", "knowledge_approval",
                        "memory_trusted_commenters")
            if _has_column(table, c)
        ]
        if not present:
            continue
        with op.batch_alter_table(table) as batch:
            for column in present:
                batch.drop_column(column)
    if _has_table(_MEMORIES):
        op.drop_index("ix_review_memories_scope", table_name=_MEMORIES)
        op.drop_table(_MEMORIES)
