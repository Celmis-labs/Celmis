"""review_policies

Revision ID: dc59cc339eb5
Revises: 067fe736b8d0
Create Date: 2026-06-18 14:02:27.354956

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'dc59cc339eb5'
down_revision: str | Sequence[str] | None = '067fe736b8d0'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Stage 10 — per-repo AI-reviewer policies (NL rules, branch filter, folder rules)."""
    op.create_table(
        "repo_review_policies",
        sa.Column("repo_slug", sa.Text(), primary_key=True),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.true(),
        ),
        sa.Column(
            "prompt_template", sa.Text(), nullable=False, server_default=sa.text("''"),
        ),
        # JSONB array of branch names. Empty / null → analyse ALL branches.
        sa.Column(
            "target_branches",
            sa.dialects.postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        # JSONB array of objects: {"pattern": "src/api/**", "prompt": "…"}.
        sa.Column(
            "folder_rules",
            sa.dialects.postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("department", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.Column("updated_by", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_repo_review_policies_department",
        "repo_review_policies",
        ["department"],
    )


def downgrade() -> None:
    op.drop_index("ix_repo_review_policies_department", table_name="repo_review_policies")
    op.drop_table("repo_review_policies")
