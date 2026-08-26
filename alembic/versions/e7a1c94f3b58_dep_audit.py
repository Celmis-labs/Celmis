"""Dependency audit runs + per-package findings.

Revision ID: e7a1c94f3b58
Revises: d9f5b3c82e41
Create Date: 2026-08-07
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "e7a1c94f3b58"
down_revision = "d9f5b3c82e41"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dep_audit_runs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("summary", JSONB(), nullable=False, server_default="{}"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_dep_runs_ws", "dep_audit_runs", ["workspace_id", "created_at"])

    op.create_table(
        "dep_findings",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("repo_slug", sa.Text(), nullable=False),
        sa.Column("ecosystem", sa.Text(), nullable=False),
        sa.Column("package", sa.Text(), nullable=False),
        sa.Column("current_version", sa.Text(), nullable=False),
        sa.Column("latest_version", sa.Text(), nullable=True),
        sa.Column("outdated", sa.Text(), nullable=False, server_default="none"),
        sa.Column("is_dev", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("vulns", JSONB(), nullable=False, server_default="[]"),
        sa.Column("severity", sa.Text(), nullable=False, server_default="none"),
        sa.Column("recommendation", sa.Text(), nullable=False, server_default="ok"),
    )
    op.create_index("ix_dep_findings_run", "dep_findings", ["run_id", "severity"])
    op.create_index("ix_dep_findings_repo", "dep_findings", ["run_id", "repo_slug"])


def downgrade() -> None:
    op.drop_index("ix_dep_findings_repo", table_name="dep_findings")
    op.drop_index("ix_dep_findings_run", table_name="dep_findings")
    op.drop_table("dep_findings")
    op.drop_index("ix_dep_runs_ws", table_name="dep_audit_runs")
    op.drop_table("dep_audit_runs")
