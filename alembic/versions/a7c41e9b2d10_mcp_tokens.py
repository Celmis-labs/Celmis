"""mcp_tokens: per-person MCP tokens issued by the superadmin.

One row per issued token (the JWT's ``jti``) with the repo list, write switch,
expiry, revocation and last use. The token value itself is never stored. OAuth
consent needs an ``oauth_grant`` row of the same table.

Idempotent: a table that is already there (a database created from the models)
is left alone.

Revision ID: a7c41e9b2d10
Revises: c1f6b8d0e256
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "a7c41e9b2d10"
down_revision = "c1f6b8d0e256"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if _has_table("mcp_tokens"):
        return
    op.create_table(
        "mcp_tokens",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False, server_default="pat"),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("issued_by", sa.Text(), nullable=False, server_default=""),
        sa.Column("label", sa.Text(), nullable=False, server_default=""),
        sa.Column("repo_patterns", JSONB(), nullable=False, server_default="[]"),
        sa.Column("allow_write", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("scopes", JSONB(), nullable=False, server_default="[]"),
        sa.Column("profile", sa.Text(), nullable=False, server_default="dev"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.Text(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_ip", sa.Text(), nullable=True),
    )
    op.create_index("ix_mcp_tokens_ws_user", "mcp_tokens", ["workspace_id", "user_id"])
    op.create_index("ix_mcp_tokens_user", "mcp_tokens", ["user_id"])


def downgrade() -> None:
    if not _has_table("mcp_tokens"):
        return
    op.drop_index("ix_mcp_tokens_user", table_name="mcp_tokens")
    op.drop_index("ix_mcp_tokens_ws_user", table_name="mcp_tokens")
    op.drop_table("mcp_tokens")
