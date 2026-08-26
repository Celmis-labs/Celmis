"""automation_runs — what was asked of the Celmis agent, and what it started

Revision ID: e1c7d4a90b35
Revises: d4a7f1c93e60
Create Date: 2026-08-18

The automation page kept the question and the plan in React state, so leaving
the page discarded both — while the work carried on in the background with
nothing on screen to say so.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "e1c7d4a90b35"
down_revision = "d4a7f1c93e60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "automation_runs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("user_id", sa.Text(), nullable=True),
        sa.Column("user_email", sa.Text(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=True),
        sa.Column("arguments", JSONB(), nullable=False, server_default="{}"),
        sa.Column("resolved_repos", JSONB(), nullable=False, server_default="[]"),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("blocked", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="planned"),
        sa.Column("result", JSONB(), nullable=False, server_default="{}"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_automation_runs_ws", "automation_runs",
                    ["workspace_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_automation_runs_ws", table_name="automation_runs")
    op.drop_table("automation_runs")
