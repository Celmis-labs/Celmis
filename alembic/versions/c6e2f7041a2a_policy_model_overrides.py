"""policy_model_overrides

Revision ID: c6e2f7041a2a
Revises: dc59cc339eb5
Create Date: 2026-07-13 16:09:16.545416

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c6e2f7041a2a'
down_revision: str | Sequence[str] | None = 'dc59cc339eb5'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Stage 11 — per-agent model overrides on the review policy.

    Each column is nullable — NULL means "fall back to workspace default"
    (from ReviewSettings). This keeps existing rows valid without a data
    migration.
    """
    for col in (
        "architect_model", "security_model", "quality_model",
        "tests_model", "verifier_model",
    ):
        op.add_column(
            "repo_review_policies",
            sa.Column(col, sa.Text(), nullable=True),
        )


def downgrade() -> None:
    for col in (
        "architect_model", "security_model", "quality_model",
        "tests_model", "verifier_model",
    ):
        op.drop_column("repo_review_policies", col)
