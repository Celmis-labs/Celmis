"""Incoming monitoring alerts (Grafana/generic ingest).

Revision ID: d9f5b3c82e41
Revises: c8e4f2a91d37
Create Date: 2026-08-07
"""

import sqlalchemy as sa

from alembic import op

revision = "d9f5b3c82e41"
down_revision = "c8e4f2a91d37"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "incoming_alerts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("source", sa.Text(), nullable=False, server_default="generic"),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("severity", sa.Text(), nullable=False, server_default="warning"),
        sa.Column("status", sa.Text(), nullable=False, server_default="new"),
        sa.Column("repo_hint", sa.Text(), nullable=True),
        sa.Column("session_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_alerts_ws", "incoming_alerts", ["workspace_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_alerts_ws", table_name="incoming_alerts")
    op.drop_table("incoming_alerts")
