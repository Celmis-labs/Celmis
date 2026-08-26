"""stage19_oauth_and_workspaces

Revision ID: c9d8e1a5f612
Revises: b5f83a1c9d02
Create Date: 2026-07-17 09:00:00.000000

Stage 19 — OAuth 2.1 auth-code + PKCE server + multi-tenant workspaces.

Tables:
  * oauth_clients — registered MCP clients (Claude Code, Cursor, custom).
  * oauth_auth_codes — short-TTL codes exchanged for tokens.
  * workspaces — multi-tenant tenancy boundary.
  * workspace_members — user ↔ workspace with role.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c9d8e1a5f612"
down_revision: str | Sequence[str] | None = "b5f83a1c9d02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ─── OAuth ────────────────────────────────────────────────────
    op.create_table(
        "oauth_clients",
        sa.Column("client_id", sa.Text(), primary_key=True),
        sa.Column("client_secret_hash", sa.Text(), nullable=True),
        # Null = public client (PKCE-only, no secret exchange).
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("redirect_uris", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("allowed_scopes", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("created_by", sa.Text(), nullable=True),
    )

    op.create_table(
        "oauth_auth_codes",
        sa.Column("code", sa.Text(), primary_key=True),
        sa.Column("client_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("code_challenge", sa.Text(), nullable=False),
        sa.Column("code_challenge_method", sa.Text(), nullable=False, server_default="S256"),
        sa.Column("scope", sa.Text(), nullable=False, server_default=""),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_oauth_codes_expires", "oauth_auth_codes", ["expires_at"])

    # ─── Workspaces ───────────────────────────────────────────────
    op.create_table(
        "workspaces",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False, unique=True),
        sa.Column("slug", sa.Text(), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("created_by", sa.Text(), nullable=True),
    )

    op.create_table(
        "workspace_members",
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False, server_default="member"),
        # owner | admin | member | viewer
        sa.Column("added_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("workspace_id", "user_id"),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="CASCADE",
        ),
    )
    op.create_index("ix_workspace_members_user", "workspace_members", ["user_id"])

    # Add workspace_id to core scoped tables. Nullable + default "default"
    # so existing rows survive. New rows written by workspace-aware code
    # get a real id.
    for tbl in ("projects", "repo_review_policies", "notification_channels",
                "deprecated_symbols", "teams"):
        op.add_column(tbl, sa.Column(
            "workspace_id", sa.Text(),
            nullable=False, server_default="default",
        ))
        op.create_index(f"ix_{tbl}_workspace", tbl, ["workspace_id"])

    # Seed a default workspace so foreign-key-ish semantics hold.
    op.execute(
        "INSERT INTO workspaces (id, name, slug, description, created_by) "
        "VALUES ('default', 'Default', 'default', "
        "        'Auto-created for pre-multitenancy rows', 'system')"
    )


def downgrade() -> None:
    for tbl in ("projects", "repo_review_policies", "notification_channels",
                "deprecated_symbols", "teams"):
        op.drop_index(f"ix_{tbl}_workspace", table_name=tbl)
        op.drop_column(tbl, "workspace_id")
    op.drop_index("ix_workspace_members_user", table_name="workspace_members")
    op.drop_table("workspace_members")
    op.drop_table("workspaces")
    op.drop_index("ix_oauth_codes_expires", table_name="oauth_auth_codes")
    op.drop_table("oauth_auth_codes")
    op.drop_table("oauth_clients")
