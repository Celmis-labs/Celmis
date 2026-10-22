"""repo sharing: one table and two nullable columns

Lets a workspace let ONE other workspace read a repository of its own for
research, for a fixed time. Nothing here grants anything by itself:

  * ``repo_shares``            — the owner's consent: receiver, repository (with
    the identity it had when it was shared), expiry, extra hidden paths, whether
    ``ask`` is allowed, how many people may hold it, whom the owner switched off;
  * ``mcp_tokens.shared_repos`` — the ids of the shares one token may read, kept
    apart from ``repo_patterns`` so that no glob can ever name a shared
    repository;
  * ``mcp_call_log.shares``     — the ids of the shares a call read through (ids
    only, never the slug of the shared repository).

Purely additive and idempotent: a table or column that is already there (a
database created from the models) is left alone. Downgrade removes exactly
these, and the code before this revision does not read any of them, so a token
that carries shares simply loses them.

Revision ID: 017a06ecb85d
Revises: e5b8c2d9a1f6
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "017a06ecb85d"
down_revision = "e5b8c2d9a1f6"
branch_labels = None
depends_on = None

_LIVE = "revoked_at IS NULL"


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return False
    return column in {c["name"] for c in inspector.get_columns(table)}


def _index_names(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return set()
    return {i["name"] for i in inspector.get_indexes(table)}


def upgrade() -> None:
    if not _has_table("repo_shares"):
        op.create_table(
            "repo_shares",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("owner_workspace_id", sa.Text(), nullable=False),
            sa.Column("grantee_workspace_id", sa.Text(), nullable=False),
            sa.Column("repo_slug", sa.Text(), nullable=False),
            sa.Column("repo_provider", sa.Text(), nullable=False),
            sa.Column("repo_full_name", sa.Text(), nullable=False),
            sa.Column("deny_globs", JSONB(), nullable=False, server_default="[]"),
            sa.Column("allow_ask", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("max_people", sa.Integer(), nullable=False, server_default="5"),
            sa.Column("blocked_user_ids", JSONB(), nullable=False, server_default="[]"),
            sa.Column("created_by", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revoked_by", sa.Text(), nullable=True),
            sa.Column("revoke_reason", sa.Text(), nullable=True),
            sa.CheckConstraint("owner_workspace_id <> grantee_workspace_id",
                               name="owner_differs_from_grantee"),
            sa.CheckConstraint("max_people BETWEEN 1 AND 25", name="max_people_range"),
        )
    have = _index_names("repo_shares")
    if "ix_repo_shares_owner" not in have:
        op.create_index("ix_repo_shares_owner", "repo_shares", ["owner_workspace_id"])
    if "ix_repo_shares_grantee" not in have:
        op.create_index("ix_repo_shares_grantee", "repo_shares",
                        ["grantee_workspace_id", "revoked_at"])
    if "uq_repo_shares_live" not in have:
        op.create_index(
            "uq_repo_shares_live", "repo_shares",
            ["owner_workspace_id", "grantee_workspace_id", "repo_slug"], unique=True,
            postgresql_where=sa.text(_LIVE), sqlite_where=sa.text(_LIVE),
        )

    if not _has_column("mcp_tokens", "shared_repos"):
        op.add_column("mcp_tokens", sa.Column("shared_repos", JSONB(), nullable=True))
    if not _has_column("mcp_call_log", "shares"):
        op.add_column("mcp_call_log", sa.Column("shares", JSONB(), nullable=True))


def downgrade() -> None:
    if _has_column("mcp_call_log", "shares"):
        with op.batch_alter_table("mcp_call_log") as batch:
            batch.drop_column("shares")
    if _has_column("mcp_tokens", "shared_repos"):
        with op.batch_alter_table("mcp_tokens") as batch:
            batch.drop_column("shared_repos")
    if _has_table("repo_shares"):
        have = _index_names("repo_shares")
        for index in ("uq_repo_shares_live", "ix_repo_shares_grantee", "ix_repo_shares_owner"):
            if index in have:
                op.drop_index(index, table_name="repo_shares")
        op.drop_table("repo_shares")
