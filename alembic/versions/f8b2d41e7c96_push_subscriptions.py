"""push subscriptions — one row per browser that opted in

Revision ID: f8b2d41e7c96
Revises: d7a1c93e5f28
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f8b2d41e7c96"
down_revision: str | Sequence[str] | None = "d7a1c93e5f28"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "push_subscriptions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.Text(), nullable=False, server_default="default"),
        # Unique: the endpoint IS the device. Re-subscribing must update the
        # row, or every notification arrives once per stale duplicate.
        sa.Column("endpoint", sa.Text(), nullable=False, unique=True),
        sa.Column("p256dh", sa.Text(), nullable=False),
        sa.Column("auth", sa.Text(), nullable=False),
        sa.Column("user_agent", sa.Text(), nullable=False, server_default=""),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_push_subs_user", "push_subscriptions", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_push_subs_user", table_name="push_subscriptions")
    op.drop_table("push_subscriptions")
