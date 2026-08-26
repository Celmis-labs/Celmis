"""policy_mcp_sources

Revision ID: a8c1e4f27b93
Revises: f14b8a2c9d01
Create Date: 2026-07-14 10:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a8c1e4f27b93"
down_revision: str | Sequence[str] | None = "f14b8a2c9d01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Stage 13 — MCP evidence sources per repo.

    Each entry: {name, url, auth_type, api_key_ref, allowed_tools,
    trigger_patterns}. `api_key_ref` points at a credentials store row
    (provider="mcp:<name>", label=<repo_slug>) so raw secrets never live
    in this table.
    """
    op.add_column(
        "repo_review_policies",
        sa.Column(
            "mcp_sources",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("repo_review_policies", "mcp_sources")
