"""stage15_intelligence_and_notifications

Revision ID: e7a92b4c8f01
Revises: d3f75c1a0e42
Create Date: 2026-07-14 16:00:00.000000

Stage 15 — cross-repo intelligence + notification channels.

Tables:
  * notification_channels   — Grafana-style contact points (Slack/Discord/
    Google Chat/webhook). Referenced by event bindings + notifiers.
  * channel_bindings        — per-repo per-event routing to channels.
  * ownership_snapshots     — git-blame + CODEOWNERS-derived ownership per
    repo path/symbol. Rebuilt on demand or via scheduled job.
  * repo_summaries          — auto-generated architecture summary per repo.
  * deprecated_symbols      — deprecated API surface entries + consumers scan.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e7a92b4c8f01"
down_revision: str | Sequence[str] | None = "d3f75c1a0e42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ─── notification_channels ────────────────────────────────────────
    op.create_table(
        "notification_channels",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False, unique=True),
        sa.Column("kind", sa.Text(), nullable=False),
        # kind: 'slack' | 'discord' | 'google_chat' | 'webhook'
        sa.Column("webhook_url", sa.Text(), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=False, server_default=sa.text("'{}'::jsonb")),
        # Extra kind-specific config (thread_id, headers, template).
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("created_by", sa.Text(), nullable=True),
    )

    op.create_table(
        "channel_bindings",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("repo_slug", sa.Text(), nullable=True),
        # NULL = workspace-wide binding.
        sa.Column("event", sa.Text(), nullable=False),
        # event: 'review_complete' | 'breaking_change' | 'compliance_failed'
        #        | 'deprecation_used' | 'apply_fix_applied' | '*'
        sa.Column("min_severity", sa.Text(), nullable=False,
                  server_default="info"),
        sa.Column("enabled", sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.ForeignKeyConstraint(
            ["channel_id"], ["notification_channels.id"], ondelete="CASCADE",
        ),
    )
    op.create_index("ix_channel_bindings_repo_event",
                    "channel_bindings", ["repo_slug", "event"])

    # ─── ownership_snapshots ──────────────────────────────────────────
    op.create_table(
        "ownership_snapshots",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("repo_slug", sa.Text(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("computed_by", sa.Text(), nullable=True),
        sa.Column("lookback_days", sa.Integer(), nullable=False,
                  server_default="90"),
        sa.Column("paths", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=False, server_default=sa.text("'{}'::jsonb")),
        # paths: {"src/foo.py": {"top_authors": [{"name":"X", "commits":12}],
        #                        "codeowners": ["@team-a"], "primary_owner": "X"}}
        sa.Column("stats", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.create_index("ix_ownership_repo", "ownership_snapshots", ["repo_slug"])

    # ─── repo_summaries ───────────────────────────────────────────────
    op.create_table(
        "repo_summaries",
        sa.Column("repo_slug", sa.Text(), primary_key=True),
        sa.Column("summary_md", sa.Text(), nullable=False),
        sa.Column("model_used", sa.Text(), nullable=True),
        sa.Column("token_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("computed_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("computed_by", sa.Text(), nullable=True),
    )

    # ─── deprecated_symbols ───────────────────────────────────────────
    op.create_table(
        "deprecated_symbols",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("repo_slug", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        # symbol: "module.function" or "GET /api/users/{id}"
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("replacement", sa.Text(), nullable=True),
        sa.Column("target_removal_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deprecated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("deprecated_by", sa.Text(), nullable=True),
        sa.Column("last_scan_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumers", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=False, server_default=sa.text("'[]'::jsonb")),
        # [{"repo_slug":"x/y","file":"a.py","line":42}]
    )
    op.create_index("ix_deprecated_repo_symbol",
                    "deprecated_symbols", ["repo_slug", "symbol"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_deprecated_repo_symbol", table_name="deprecated_symbols")
    op.drop_table("deprecated_symbols")
    op.drop_table("repo_summaries")
    op.drop_index("ix_ownership_repo", table_name="ownership_snapshots")
    op.drop_table("ownership_snapshots")
    op.drop_index("ix_channel_bindings_repo_event", table_name="channel_bindings")
    op.drop_table("channel_bindings")
    op.drop_table("notification_channels")
