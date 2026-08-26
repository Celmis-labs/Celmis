"""policy_agent_prompt_overrides

Revision ID: f14b8a2c9d01
Revises: c6e2f7041a2a
Create Date: 2026-07-14 09:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f14b8a2c9d01"
down_revision: str | Sequence[str] | None = "c6e2f7041a2a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Stage 12 — per-repo per-agent system_prompt overrides.

    Stored as JSONB `{agent_name: override_text}` for the five review agents.
    Missing key or empty value → fall back to /admin/agents global override
    → then to the agent's built-in default.
    """
    op.add_column(
        "repo_review_policies",
        sa.Column(
            "agent_prompt_overrides",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("repo_review_policies", "agent_prompt_overrides")
