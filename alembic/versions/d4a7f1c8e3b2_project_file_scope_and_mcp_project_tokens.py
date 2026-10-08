"""project_repos file scope + mcp_project_tokens

Two additions for code added from an archive and shared with outside tools:

  * ``project_repos.include_globs`` / ``exclude_globs`` — which files of a
    repository a project looks at (empty include = all, exclude wins);
  * ``mcp_project_tokens`` — opaque, hashed, expiring bearer tokens that can
    search exactly one project.

Idempotent: a column or table that is already there (a database created from
the models) is left alone. Downgrade removes exactly what the upgrade made.

Revision ID: d4a7f1c8e3b2
Revises: b3e9d27f5a40
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "d4a7f1c8e3b2"
down_revision = "b3e9d27f5a40"
branch_labels = None
depends_on = None

_TABLE = "mcp_project_tokens"


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return False
    return column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    for column in ("include_globs", "exclude_globs"):
        if not _has_column("project_repos", column):
            op.add_column(
                "project_repos",
                sa.Column(column, JSONB(), nullable=False, server_default="[]"),
            )
    if _has_table(_TABLE):
        return
    op.create_table(
        "mcp_project_tokens",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("token_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("project_id", sa.dialects.postgresql.UUID(as_uuid=False),
                  sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("label", sa.Text(), nullable=False, server_default=""),
        sa.Column("scopes", JSONB(), nullable=False, server_default="[]"),
        sa.Column("created_by", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_mcp_project_tokens_project", _TABLE, ["project_id"])


def downgrade() -> None:
    if _has_table(_TABLE):
        op.drop_index("ix_mcp_project_tokens_project", table_name=_TABLE)
        op.drop_table(_TABLE)
    for column in ("exclude_globs", "include_globs"):
        if _has_column("project_repos", column):
            with op.batch_alter_table("project_repos") as batch:
                batch.drop_column(column)
