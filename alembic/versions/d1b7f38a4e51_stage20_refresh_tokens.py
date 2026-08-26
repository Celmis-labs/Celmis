"""stage20_refresh_tokens

Revision ID: d1b7f38a4e51
Revises: c9d8e1a5f612
Create Date: 2026-07-17 14:00:00.000000

Refresh tokens for OAuth 2.1 with rotation:
    - Each row = one active refresh token, hashed (never stored raw).
    - `rotated_to` points at the new row when this token is exchanged
      → allows detection of a stolen-then-reused token (family reuse
      → invalidate whole chain).
    - TTL is longer than access token; typical 30 days.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d1b7f38a4e51"
down_revision: str | Sequence[str] | None = "c9d8e1a5f612"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oauth_refresh_tokens",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("token_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("client_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False, server_default=""),
        sa.Column("family_id", sa.Text(), nullable=False),
        # Root of the rotation chain — reuse of any consumed token in
        # this family kills every descendant.
        sa.Column("issued_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rotated_to", sa.Text(), nullable=True),
        # Set when this token is exchanged for a new one.
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_refresh_family", "oauth_refresh_tokens", ["family_id"])
    op.create_index("ix_refresh_user_client",
                    "oauth_refresh_tokens", ["user_id", "client_id"])


def downgrade() -> None:
    op.drop_index("ix_refresh_user_client", table_name="oauth_refresh_tokens")
    op.drop_index("ix_refresh_family", table_name="oauth_refresh_tokens")
    op.drop_table("oauth_refresh_tokens")
