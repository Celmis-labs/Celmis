"""repo_access_rules

Revision ID: f2c3a9d17b40
Revises: e4a2c7b9f103
Create Date: 2026-07-18 12:00:00.000000

Stage 22 — fine-grained *research* visibility rules per (workspace, team,
repo). Governs what a team may learn through Q&A / graph / vector search,
down to individual paths (deny-globs for creds / crypto / DB-connection).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "f2c3a9d17b40"
down_revision: str | Sequence[str] | None = "e4a2c7b9f103"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "repo_access_rules",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("team_id", sa.Text(), nullable=False),
        sa.Column("repo_slug", sa.Text(), nullable=False),
        sa.Column("visibility", sa.Text(), nullable=False, server_default="code"),
        sa.Column("allow_globs", JSONB(), nullable=False, server_default="[]"),
        sa.Column("deny_globs", JSONB(), nullable=False, server_default="[]"),
        sa.Column("sensitivity_tags", JSONB(), nullable=False, server_default="[]"),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id", "team_id", "repo_slug", name="uq_repo_access_rule",
        ),
    )
    op.create_index(
        "ix_repo_access_rules_repo", "repo_access_rules",
        ["workspace_id", "repo_slug"],
    )


def downgrade() -> None:
    op.drop_index("ix_repo_access_rules_repo", table_name="repo_access_rules")
    op.drop_table("repo_access_rules")
