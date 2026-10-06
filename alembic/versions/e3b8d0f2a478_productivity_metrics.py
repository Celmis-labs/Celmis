"""productivity metrics: pull request history, deployments, sync progress

Six tables, nothing else touched. They are filled by `src/productivity` (AGPL,
opt-in per repository) and read by the Enterprise metrics API.

The migration is self-contained: it repeats the column definitions rather than
importing the models, so a later model change cannot rewrite what this one did.

Revision ID: e3b8d0f2a478
Revises: d2a7c9e1f367
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e3b8d0f2a478"
down_revision: str | Sequence[str] | None = "d2a7c9e1f367"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TS = sa.DateTime(timezone=True)


def _ts(name: str, *, nullable: bool = True, now: bool = False) -> sa.Column:
    return sa.Column(
        name, _TS, nullable=nullable, server_default=sa.func.now() if now else None)


def _flag(name: str, default: str = "false") -> sa.Column:
    return sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.text(default))


def upgrade() -> None:
    op.create_table(
        "productivity_repo_settings",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("provider", sa.Text(), nullable=False, server_default=""),
        sa.Column("repo", sa.Text(), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=True),
        sa.Column("backfill_days", sa.Integer(), nullable=True),
        sa.Column("production_branches", postgresql.JSONB(), nullable=True),
        sa.Column("integration_branches", postgresql.JSONB(), nullable=True),
        sa.Column("deploy_source", sa.Text(), nullable=True),
        sa.Column("tag_pattern", sa.Text(), nullable=True),
        sa.Column("revert_patterns", postgresql.JSONB(), nullable=True),
        sa.Column("hotfix_branch_patterns", postgresql.JSONB(), nullable=True),
        sa.Column("bugfix_patterns", postgresql.JSONB(), nullable=True),
        sa.Column("ignored_authors", postgresql.JSONB(), nullable=True),
        sa.Column("bot_markers", postgresql.JSONB(), nullable=True),
        sa.Column("failure_window_days", sa.Integer(), nullable=True),
        sa.Column("deploy_group_minutes", sa.Integer(), nullable=True),
        sa.Column("rate_per_hour", sa.Integer(), nullable=True),
        _ts("created_at", nullable=False, now=True),
        _ts("updated_at", nullable=False, now=True),
        sa.UniqueConstraint("workspace_id", "provider", "repo",
                            name="uq_productivity_repo_settings"),
    )

    op.create_table(
        "productivity_pull_requests",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("repo_slug", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("author_key", sa.Text(), nullable=True),
        sa.Column("author_name", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False, server_default="open"),
        _flag("is_draft"),
        sa.Column("source_branch", sa.Text(), nullable=True),
        sa.Column("target_branch", sa.Text(), nullable=True),
        _ts("created_at"),
        _ts("updated_on"),
        _ts("first_commit_at"),
        _ts("first_review_at"),
        _ts("first_approval_at"),
        _ts("merged_at"),
        _flag("merged_at_approx"),
        _ts("closed_at"),
        sa.Column("merge_commit_sha", sa.Text(), nullable=True),
        sa.Column("head_sha", sa.Text(), nullable=True),
        sa.Column("additions", sa.Integer(), nullable=True),
        sa.Column("deletions", sa.Integer(), nullable=True),
        sa.Column("files_changed", sa.Integer(), nullable=True),
        sa.Column("commits_count", sa.Integer(), nullable=True),
        sa.Column("human_comments", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("approvals", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("kind", sa.Text(), nullable=False, server_default="feature"),
        sa.Column("reverts_pr_number", sa.Integer(), nullable=True),
        sa.Column("revert_hint", sa.Text(), nullable=True),
        sa.Column("ticket_key", sa.Text(), nullable=True),
        sa.Column("commit_shas", postgresql.JSONB(), nullable=True),
        _ts("prod_deployed_at"),
        sa.Column("prod_deployment_id", sa.Text(), nullable=True),
        sa.Column("deploy_link", sa.Text(), nullable=True),
        sa.Column("detail_state", sa.Text(), nullable=False, server_default="none"),
        _ts("detail_synced_at"),
        sa.UniqueConstraint("workspace_id", "provider", "repo", "number",
                            name="uq_productivity_pull_request"),
    )
    op.create_index("ix_productivity_prs_ws_repo_state", "productivity_pull_requests",
                    ["workspace_id", "repo", "state", "merged_at"])
    op.create_index("ix_productivity_prs_ws_created", "productivity_pull_requests",
                    ["workspace_id", "created_at"])
    op.create_index("ix_productivity_prs_ws_repo_updated", "productivity_pull_requests",
                    ["workspace_id", "repo", "updated_on"])
    op.create_index("ix_productivity_prs_ws_author", "productivity_pull_requests",
                    ["workspace_id", "author_key"])

    op.create_table(
        "productivity_pr_events",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("pr_id", sa.Text(),
                  sa.ForeignKey("productivity_pull_requests.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("actor_key", sa.Text(), nullable=False, server_default=""),
        sa.Column("actor_name", sa.Text(), nullable=True),
        _ts("at", nullable=False),
        _flag("is_bot"),
        _flag("is_author"),
        sa.UniqueConstraint("pr_id", "kind", "external_id", name="uq_productivity_pr_event"),
    )
    op.create_index("ix_productivity_pr_events_actor", "productivity_pr_events",
                    ["workspace_id", "actor_key", "at"])

    op.create_table(
        "productivity_deployments",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("environment", sa.Text(), nullable=False, server_default="production"),
        sa.Column("branch", sa.Text(), nullable=True),
        sa.Column("sha", sa.Text(), nullable=True),
        _ts("deployed_at", nullable=False),
        sa.Column("source", sa.Text(), nullable=False, server_default="merge"),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="success"),
        _flag("is_failure"),
        sa.Column("failed_by_pr_number", sa.Integer(), nullable=True),
        _ts("recovered_at"),
        sa.UniqueConstraint("workspace_id", "provider", "repo", "source", "external_id",
                            name="uq_productivity_deployment"),
    )
    op.create_index("ix_productivity_deployments_repo_at", "productivity_deployments",
                    ["workspace_id", "repo", "deployed_at"])

    op.create_table(
        "productivity_deployment_prs",
        sa.Column("deployment_id", sa.Text(),
                  sa.ForeignKey("productivity_deployments.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("pr_number", sa.Integer(), primary_key=True),
    )

    op.create_table(
        "productivity_sync_state",
        sa.Column("workspace_id", sa.Text(), primary_key=True),
        sa.Column("provider", sa.Text(), primary_key=True),
        sa.Column("repo", sa.Text(), primary_key=True),
        _ts("updated_watermark"),
        _ts("backfill_from"),
        _flag("backfill_done"),
        _ts("deploy_watermark"),
        _ts("last_run_at"),
        _ts("last_ok_at"),
        sa.Column("last_error", sa.Text(), nullable=True),
        _ts("rate_limited_until"),
        _ts("running_until"),
        sa.Column("prs_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("prs_detailed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("prs_pending", sa.Integer(), nullable=False, server_default="0"),
        _ts("updated_at", nullable=False, now=True),
    )


def downgrade() -> None:
    op.drop_table("productivity_sync_state")
    op.drop_table("productivity_deployment_prs")
    op.drop_index("ix_productivity_deployments_repo_at", table_name="productivity_deployments")
    op.drop_table("productivity_deployments")
    op.drop_index("ix_productivity_pr_events_actor", table_name="productivity_pr_events")
    op.drop_table("productivity_pr_events")
    for ix in ("ix_productivity_prs_ws_author", "ix_productivity_prs_ws_repo_updated",
               "ix_productivity_prs_ws_created", "ix_productivity_prs_ws_repo_state"):
        op.drop_index(ix, table_name="productivity_pull_requests")
    op.drop_table("productivity_pull_requests")
    op.drop_table("productivity_repo_settings")
