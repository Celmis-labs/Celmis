"""Stage 23 — finding feedback, password reset tokens, workspace invites

Revision ID: b7f4c2e908a1
Revises: a3d9e5c71b28
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "b7f4c2e908a1"
down_revision = "a3d9e5c71b28"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "finding_feedback",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("finding_key", sa.Text(), nullable=False),
        sa.Column("repo_slug", sa.Text(), nullable=True),
        sa.Column("agent", sa.Text(), nullable=True),
        sa.Column("severity", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("user_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("run_id", "finding_key", name="uq_finding_feedback"),
    )
    op.create_index("ix_finding_feedback_run", "finding_feedback", ["run_id"])
    op.create_index("ix_finding_feedback_agent", "finding_feedback", ["agent", "state"])

    op.create_table(
        "password_reset_tokens",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("token_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_reset_user", "password_reset_tokens", ["user_id"])

    op.create_table(
        "workspace_invites",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("role", sa.Text(), nullable=False, server_default="member"),
        sa.Column("max_uses", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("used_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_invite_workspace", "workspace_invites", ["workspace_id"])


def downgrade() -> None:
    op.drop_index("ix_invite_workspace", table_name="workspace_invites")
    op.drop_table("workspace_invites")
    op.drop_index("ix_reset_user", table_name="password_reset_tokens")
    op.drop_table("password_reset_tokens")
    op.drop_index("ix_finding_feedback_agent", table_name="finding_feedback")
    op.drop_index("ix_finding_feedback_run", table_name="finding_feedback")
    op.drop_table("finding_feedback")
