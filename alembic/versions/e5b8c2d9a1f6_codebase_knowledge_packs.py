"""codebase: knowledge packs, their versions, items, grants and invites

Seven tables for the Codebase section (packs of skills, API examples,
checklists and notes that a workspace curates and its agents read), plus one
column on ``mcp_call_log``:

  * ``knowledge_packs``         — the pack, its audience and active version;
  * ``knowledge_pack_teams``    — teams a pack is granted to;
  * ``knowledge_pack_members``  — people named on a pack (by user id);
  * ``knowledge_pack_versions`` — immutable snapshots, numbered per pack;
  * ``knowledge_items``         — the files of a version;
  * ``knowledge_pack_invites``  — hashed, expiring links that add a person;
  * ``knowledge_pack_tokens``   — hashed, expiring tokens that publish a pack;
  * ``mcp_call_log.packs``      — ids of the packs an MCP call read from.

Idempotent: a table or column that is already there (a database created from
the models) is left alone. Downgrade removes exactly what the upgrade made.
The children of a pack cascade in the database, but nothing in the code relies
on it: the store deletes them explicitly.

Revision ID: e5b8c2d9a1f6
Revises: d4a7f1c8e3b2
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "e5b8c2d9a1f6"
down_revision = "d4a7f1c8e3b2"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return False
    return column in {c["name"] for c in inspector.get_columns(table)}


def _pack_fk() -> sa.ForeignKey:
    return sa.ForeignKey("knowledge_packs.id", ondelete="CASCADE")


def upgrade() -> None:
    if not _has_table("knowledge_packs"):
        op.create_table(
            "knowledge_packs",
            sa.Column("id", UUID(as_uuid=False), primary_key=True),
            sa.Column("workspace_id", sa.Text(), nullable=False),
            sa.Column("slug", sa.Text(), nullable=False),
            sa.Column("title", sa.Text(), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("audience", sa.Text(), nullable=False, server_default="teams"),
            sa.Column("current_version_id", UUID(as_uuid=False), nullable=True),
            sa.Column("created_by_id", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.UniqueConstraint("workspace_id", "slug",
                                name="uq_knowledge_packs_workspace_slug"),
        )
        op.create_index("ix_knowledge_packs_workspace", "knowledge_packs", ["workspace_id"])

    if not _has_table("knowledge_pack_teams"):
        op.create_table(
            "knowledge_pack_teams",
            sa.Column("pack_id", UUID(as_uuid=False), _pack_fk(), primary_key=True),
            sa.Column("team_id", sa.Text(), primary_key=True),
            sa.Column("permission", sa.Text(), nullable=False, server_default="read"),
            sa.Column("granted_by_id", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
        )
        op.create_index("ix_knowledge_pack_teams_team", "knowledge_pack_teams", ["team_id"])

    if not _has_table("knowledge_pack_members"):
        op.create_table(
            "knowledge_pack_members",
            sa.Column("pack_id", UUID(as_uuid=False), _pack_fk(), primary_key=True),
            sa.Column("user_id", sa.Text(), primary_key=True),
            sa.Column("permission", sa.Text(), nullable=False, server_default="read"),
            sa.Column("granted_by_id", sa.Text(), nullable=True),
            sa.Column("invite_id", UUID(as_uuid=False), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
        )
        op.create_index("ix_knowledge_pack_members_user", "knowledge_pack_members",
                        ["user_id"])

    if not _has_table("knowledge_pack_versions"):
        op.create_table(
            "knowledge_pack_versions",
            sa.Column("id", UUID(as_uuid=False), primary_key=True),
            sa.Column("pack_id", UUID(as_uuid=False), _pack_fk(), nullable=False),
            sa.Column("workspace_id", sa.Text(), nullable=False),
            sa.Column("number", sa.Integer(), nullable=False),
            sa.Column("note", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_by_id", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.Column("file_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("total_bytes", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("sha", sa.Text(), nullable=False, server_default=""),
            sa.UniqueConstraint("pack_id", "number",
                                name="uq_knowledge_pack_versions_pack_number"),
        )
        op.create_index("ix_knowledge_pack_versions_pack", "knowledge_pack_versions",
                        ["pack_id"])

    if not _has_table("knowledge_items"):
        op.create_table(
            "knowledge_items",
            sa.Column("id", UUID(as_uuid=False), primary_key=True),
            sa.Column("version_id", UUID(as_uuid=False),
                      sa.ForeignKey("knowledge_pack_versions.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("pack_id", UUID(as_uuid=False), _pack_fk(), nullable=False),
            sa.Column("workspace_id", sa.Text(), nullable=False),
            sa.Column("path", sa.Text(), nullable=False),
            sa.Column("kind", sa.Text(), nullable=False),
            sa.Column("skill", sa.Text(), nullable=True),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("sha", sa.Text(), nullable=False),
            sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("repos", JSONB(), nullable=False, server_default="[]"),
            sa.Column("tags", JSONB(), nullable=False, server_default="[]"),
            sa.Column("terms", JSONB(), nullable=False, server_default="{}"),
        )
        op.create_index("ix_knowledge_items_version", "knowledge_items", ["version_id"])
        op.create_index("ix_knowledge_items_pack", "knowledge_items", ["pack_id"])

    if not _has_table("knowledge_pack_invites"):
        op.create_table(
            "knowledge_pack_invites",
            sa.Column("id", UUID(as_uuid=False), primary_key=True),
            sa.Column("pack_id", UUID(as_uuid=False), _pack_fk(), nullable=False),
            sa.Column("workspace_id", sa.Text(), nullable=False),
            sa.Column("token_hash", sa.Text(), nullable=False, unique=True),
            sa.Column("permission", sa.Text(), nullable=False, server_default="read"),
            sa.Column("created_by_id", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("max_uses", sa.Integer(), nullable=False, server_default="25"),
            sa.Column("used_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_knowledge_pack_invites_pack", "knowledge_pack_invites",
                        ["pack_id"])

    if not _has_table("knowledge_pack_tokens"):
        op.create_table(
            "knowledge_pack_tokens",
            sa.Column("id", UUID(as_uuid=False), primary_key=True),
            sa.Column("pack_id", UUID(as_uuid=False), _pack_fk(), nullable=False),
            sa.Column("workspace_id", sa.Text(), nullable=False),
            sa.Column("label", sa.Text(), nullable=False, server_default=""),
            sa.Column("token_hash", sa.Text(), nullable=False, unique=True),
            sa.Column("created_by_id", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_knowledge_pack_tokens_pack", "knowledge_pack_tokens",
                        ["pack_id"])

    if not _has_column("mcp_call_log", "packs"):
        op.add_column("mcp_call_log", sa.Column("packs", JSONB(), nullable=True))


def downgrade() -> None:
    if _has_column("mcp_call_log", "packs"):
        with op.batch_alter_table("mcp_call_log") as batch:
            batch.drop_column("packs")
    # Children before the pack: the order a database without cascades needs.
    for table, index in (
        ("knowledge_pack_tokens", "ix_knowledge_pack_tokens_pack"),
        ("knowledge_pack_invites", "ix_knowledge_pack_invites_pack"),
        ("knowledge_items", "ix_knowledge_items_pack"),
        ("knowledge_items", "ix_knowledge_items_version"),
        ("knowledge_pack_versions", "ix_knowledge_pack_versions_pack"),
        ("knowledge_pack_members", "ix_knowledge_pack_members_user"),
        ("knowledge_pack_teams", "ix_knowledge_pack_teams_team"),
        ("knowledge_packs", "ix_knowledge_packs_workspace"),
    ):
        if _has_table(table):
            existing = {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(table)}
            if index in existing:
                op.drop_index(index, table_name=table)
    for table in (
        "knowledge_pack_tokens",
        "knowledge_pack_invites",
        "knowledge_items",
        "knowledge_pack_versions",
        "knowledge_pack_members",
        "knowledge_pack_teams",
        "knowledge_packs",
    ):
        if _has_table(table):
            op.drop_table(table)
