"""sync_jobs.cancel_requested — cooperative cancellation flag for long jobs.

Revision ID: d4e7f1a92c53
Revises: f3b6d8a25c19
Create Date: 2026-08-07
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "d4e7f1a92c53"
down_revision = "f3b6d8a25c19"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sync_jobs",
        sa.Column(
            "cancel_requested",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("sync_jobs", "cancel_requested")
