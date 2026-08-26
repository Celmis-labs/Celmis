"""policy_disabled_agents

Revision ID: e1c8b7a45d92
Revises: d4e7f1a92c53
Create Date: 2026-08-10 10:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e1c8b7a45d92"
down_revision: str | Sequence[str] | None = "d4e7f1a92c53"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Per-repo agent kill-switch.

    `disabled_agents` holds review-agent names (architect / security /
    quality / tests / structural) that must be skipped for this repo. The
    orchestrator filters them out before the parallel run, so a disabled
    agent makes no LLM call and produces no findings. Empty list (default)
    keeps the historical behaviour — every agent runs.
    """
    op.add_column(
        "repo_review_policies",
        sa.Column(
            "disabled_agents",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("repo_review_policies", "disabled_agents")
