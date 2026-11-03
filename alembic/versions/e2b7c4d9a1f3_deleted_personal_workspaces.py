"""remember which personal workspaces an admin deleted

Login re-creates a person's personal workspace when it is missing, so deleting one
did not stick. This table is the marker: sign-in leaves the workspace deleted while
the person belongs to another workspace.

Revision ID: e2b7c4d9a1f3
Revises: d1f6a8c3e520
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "e2b7c4d9a1f3"
down_revision = "d1f6a8c3e520"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "deleted_personal_workspaces" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "deleted_personal_workspaces",
        sa.Column("user_id", sa.Text(), primary_key=True),
        sa.Column("deleted_by", sa.Text(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("deleted_personal_workspaces")
