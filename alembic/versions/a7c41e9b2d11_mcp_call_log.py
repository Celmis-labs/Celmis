"""mcp_call_log: one audit row per MCP tool call.

Who, which token, which tool, which repos, how big the answer was. Never the
arguments' values, the result or a secret (``args_hash`` is a one-way digest).

Idempotent: a table that is already there is left alone.

Revision ID: a7c41e9b2d11
Revises: a7c41e9b2d10
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "a7c41e9b2d11"
down_revision = "a7c41e9b2d10"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if _has_table("mcp_call_log"):
        return
    op.create_table(
        "mcp_call_log",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("workspace_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("user_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("token_id", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False, server_default=""),
        sa.Column("client_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("tool", sa.Text(), nullable=False),
        sa.Column("profile", sa.Text(), nullable=False, server_default="full"),
        sa.Column("repos", JSONB(), nullable=False, server_default="[]"),
        sa.Column("status", sa.Text(), nullable=False, server_default="ok"),
        sa.Column("result_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("result_items", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("args_hash", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index("ix_mcp_call_log_ts", "mcp_call_log", ["ts"])
    op.create_index("ix_mcp_call_log_ws_ts", "mcp_call_log", ["workspace_id", "ts"])
    op.create_index("ix_mcp_call_log_token", "mcp_call_log", ["token_id", "ts"])


def downgrade() -> None:
    if not _has_table("mcp_call_log"):
        return
    op.drop_index("ix_mcp_call_log_token", table_name="mcp_call_log")
    op.drop_index("ix_mcp_call_log_ws_ts", table_name="mcp_call_log")
    op.drop_index("ix_mcp_call_log_ts", table_name="mcp_call_log")
    op.drop_table("mcp_call_log")
