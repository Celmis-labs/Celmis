"""Stage 23 — workspace LLM budgets + spend ledger

Adds:
  * workspace_budgets — per-workspace monthly cap (0 = unlimited) with an
    alert threshold and an optional hard stop.
  * llm_spend — append-only ledger written by every LLM surface (qa / review /
    embeddings) so tokens and cost can be broken down by surface, agent and
    model, including cached-input tokens and whether the cost is a real
    provider charge or an estimate.

Revision ID: a3d9e5c71b28
Revises: f2c3a9d17b40
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "a3d9e5c71b28"
down_revision = "f2c3a9d17b40"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspace_budgets",
        sa.Column("workspace_id", sa.Text(), primary_key=True),
        sa.Column("monthly_usd_cap", sa.Float(), nullable=False, server_default="0"),
        sa.Column("alert_pct", sa.Integer(), nullable=False, server_default="80"),
        sa.Column("hard_stop", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column("updated_by", sa.Text(), nullable=True),
    )

    op.create_table(
        "llm_spend",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("surface", sa.Text(), nullable=False),
        sa.Column("agent", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=False, server_default=""),
        sa.Column("provider", sa.Text(), nullable=False, server_default=""),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("cost_source", sa.Text(), nullable=False, server_default="unknown"),
        sa.Column("tokens_in", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_out", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cached_tokens_in", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("user_id", sa.Text(), nullable=True),
        sa.Column("repo_slug", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_llm_spend_ws_time", "llm_spend", ["workspace_id", "created_at"])
    op.create_index("ix_llm_spend_surface", "llm_spend", ["surface"])


def downgrade() -> None:
    op.drop_index("ix_llm_spend_surface", table_name="llm_spend")
    op.drop_index("ix_llm_spend_ws_time", table_name="llm_spend")
    op.drop_table("llm_spend")
    op.drop_table("workspace_budgets")
