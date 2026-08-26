"""compliance_and_teams

Revision ID: d3f75c1a0e42
Revises: a8c1e4f27b93
Create Date: 2026-07-14 11:00:00.000000

Stage 14 — compliance checks (first-class policy objects that hard-block
APPROVE) and RBAC/teams (workspaces of users, per-repo access grants).
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d3f75c1a0e42"
down_revision: str | Sequence[str] | None = "a8c1e4f27b93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ─── Compliance checks ───────────────────────────────────────────
    op.create_table(
        "compliance_checks",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("scope", sa.Text(), nullable=False, server_default="workspace"),
        # scope: 'workspace' | 'repo:<slug>'
        sa.Column("glob_pattern", sa.Text(), nullable=False, server_default="**"),
        sa.Column("rule", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False, server_default="error"),
        # severity: 'error' (blocks APPROVE) | 'warn' (surfaces only)
        sa.Column("blocking", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("created_by", sa.Text(), nullable=True),
    )
    op.create_index("ix_compliance_scope", "compliance_checks", ["scope"])

    # ─── Teams / RBAC ────────────────────────────────────────────────
    op.create_table(
        "teams",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "team_members",
        sa.Column("team_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False, server_default="member"),
        # role: 'owner' | 'admin' | 'reviewer' | 'member' | 'viewer'
        sa.Column("added_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("team_id", "user_id"),
        sa.ForeignKeyConstraint(
            ["team_id"], ["teams.id"], ondelete="CASCADE",
        ),
    )
    op.create_index("ix_team_members_user", "team_members", ["user_id"])

    op.create_table(
        "repo_team_access",
        sa.Column("repo_slug", sa.Text(), nullable=False),
        sa.Column("team_id", sa.Text(), nullable=False),
        sa.Column("permission", sa.Text(), nullable=False, server_default="review"),
        # permission: 'admin' | 'review' | 'read'
        sa.PrimaryKeyConstraint("repo_slug", "team_id"),
        sa.ForeignKeyConstraint(
            ["team_id"], ["teams.id"], ondelete="CASCADE",
        ),
    )
    op.create_index("ix_repo_team_access_repo", "repo_team_access", ["repo_slug"])


def downgrade() -> None:
    op.drop_index("ix_repo_team_access_repo", table_name="repo_team_access")
    op.drop_table("repo_team_access")
    op.drop_index("ix_team_members_user", table_name="team_members")
    op.drop_table("team_members")
    op.drop_table("teams")
    op.drop_index("ix_compliance_scope", table_name="compliance_checks")
    op.drop_table("compliance_checks")
