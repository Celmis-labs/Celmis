"""Resource/usage sampling history (RAM, CPU, parallel reviews, LLM calls).

Revision ID: f3b6d8a25c19
Revises: e7a1c94f3b58
Create Date: 2026-08-07
"""

import sqlalchemy as sa

from alembic import op

revision = "f3b6d8a25c19"
down_revision = "e7a1c94f3b58"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "resource_samples",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("cpu_pct", sa.Float(), nullable=False, server_default="0"),
        sa.Column("rss_mb", sa.Float(), nullable=False, server_default="0"),
        sa.Column("sys_mem_pct", sa.Float(), nullable=False, server_default="0"),
        sa.Column("load1", sa.Float(), nullable=False, server_default="0"),
        sa.Column("reviews_running", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("jobs_running", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("jobs_pending", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("agent_sessions_running", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("llm_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("llm_tokens_in", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("llm_tokens_out", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("http_requests", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_resource_samples_ts", "resource_samples", ["ts"])


def downgrade() -> None:
    op.drop_index("ix_resource_samples_ts", table_name="resource_samples")
    op.drop_table("resource_samples")
