"""Knowledge packs: per-workspace limits, kept front matter, semantic-index marker

* ``workspace_pack_limits`` - one row per workspace; every column NULL = inherit
  the install default, then the built-in.
* ``knowledge_items.meta`` / ``status`` - the whole YAML front matter of a
  markdown file, and its normalised ``status``.
* ``knowledge_pack_versions.indexed_signature`` - the embeddings signature a
  version was indexed under for semantic search (NULL = not indexed).

Idempotent; the downgrade drops what the upgrade added.

Revision ID: c7e1a9d4b2f8
Revises: b4d8f2a6c1e7
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "c7e1a9d4b2f8"
down_revision = "b4d8f2a6c1e7"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return False
    return column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("workspace_pack_limits"):
        op.create_table(
            "workspace_pack_limits",
            sa.Column("workspace_id", sa.Text(), primary_key=True),
            sa.Column("archive_bytes", sa.BigInteger(), nullable=True),
            sa.Column("max_files", sa.Integer(), nullable=True),
            sa.Column("file_bytes", sa.Integer(), nullable=True),
            sa.Column("total_bytes", sa.BigInteger(), nullable=True),
            sa.Column("packs_per_workspace", sa.Integer(), nullable=True),
            sa.Column("keep_versions", sa.Integer(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
        )
    if not _has_column("knowledge_items", "meta"):
        op.add_column("knowledge_items", sa.Column(
            "meta", postgresql.JSONB(), nullable=False, server_default="{}"))
    if not _has_column("knowledge_items", "status"):
        op.add_column("knowledge_items", sa.Column("status", sa.Text(), nullable=True))
    if not _has_column("knowledge_pack_versions", "indexed_signature"):
        op.add_column("knowledge_pack_versions",
                      sa.Column("indexed_signature", sa.Text(), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("workspace_pack_limits"):
        op.drop_table("workspace_pack_limits")
    for table, column in (("knowledge_items", "meta"), ("knowledge_items", "status"),
                          ("knowledge_pack_versions", "indexed_signature")):
        if _has_column(table, column):
            with op.batch_alter_table(table) as batch:
                batch.drop_column(column)
