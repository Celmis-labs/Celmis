"""review issues, pull-request state, per-repo ignore globs and comment threshold

Four things the review pipeline could not say before this revision:

- which paths a repository's review should never read
  (`repo_review_policies.ignore_globs`), beyond the install-wide skip lists;
- the lowest severity worth a PR comment
  (`repo_review_policies.comment_min_severity`) — below it a finding is still
  counted and stored, just not posted;
- whether a finding survived the next commit. Findings lived only as a JSON
  blob per run, so nothing linked push N's finding to push N+1's. A
  `review_issues` row is keyed by a line-free fingerprint per pull request;
- what became of a reviewed pull request (`review_pull_requests`): open,
  merged or closed, and how its reviews went.

Literal `op.add_column` / `op.create_table` calls only — see
tests/db/test_migration_chain.py, which reads them with `ast`.

Revision ID: c4f19a7d2e83
Revises: b7e3c19a5d40
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "c4f19a7d2e83"
down_revision = "b7e3c19a5d40"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def _has_table(table: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table)


def upgrade() -> None:
    if not _has_column("repo_review_policies", "ignore_globs"):
        op.add_column(
            "repo_review_policies",
            sa.Column("ignore_globs", postgresql.JSONB(astext_type=sa.Text()),
                      nullable=True),
        )
    if not _has_column("repo_review_policies", "comment_min_severity"):
        op.add_column(
            "repo_review_policies",
            sa.Column("comment_min_severity", sa.Text(), nullable=True),
        )

    if not _has_table("review_issues"):
        op.create_table(
            "review_issues",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("workspace_id", sa.Text(), nullable=False, server_default="default"),
            sa.Column("repo_slug", sa.Text(), nullable=False),
            sa.Column("fingerprint", sa.Text(), nullable=False),
            sa.Column("file_path", sa.Text(), nullable=False, server_default=""),
            sa.Column("line", sa.Integer(), nullable=True),
            sa.Column("agent", sa.Text(), nullable=True),
            sa.Column("rule_id", sa.Text(), nullable=True),
            sa.Column("category", sa.Text(), nullable=False, server_default="other"),
            sa.Column("severity", sa.Text(), nullable=False, server_default="warning"),
            sa.Column("title", sa.Text(), nullable=False, server_default=""),
            sa.Column("body", sa.Text(), nullable=False, server_default=""),
            sa.Column("suggestion", sa.Text(), nullable=True),
            sa.Column("status", sa.Text(), nullable=False, server_default="open"),
            sa.Column("resolution_source", sa.Text(), nullable=True),
            sa.Column("pr_provider", sa.Text(), nullable=False, server_default=""),
            sa.Column("pr_repo", sa.Text(), nullable=False, server_default=""),
            sa.Column("pr_number", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("pr_url", sa.Text(), nullable=True),
            sa.Column("first_run_id", sa.Text(), nullable=True),
            sa.Column("last_run_id", sa.Text(), nullable=True),
            sa.Column("first_seen_sha", sa.Text(), nullable=True),
            sa.Column("last_seen_sha", sa.Text(), nullable=True),
            sa.Column("fixed_in_sha", sa.Text(), nullable=True),
            sa.Column("occurrences", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint(
                "workspace_id", "repo_slug", "pr_number", "fingerprint",
                name="uq_review_issue_pr_fingerprint",
            ),
        )
        op.create_index("ix_review_issues_ws_status", "review_issues",
                        ["workspace_id", "status"])
        op.create_index("ix_review_issues_ws_seen", "review_issues",
                        ["workspace_id", "first_seen_at"])
        op.create_index("ix_review_issues_pr", "review_issues",
                        ["workspace_id", "pr_provider", "pr_repo", "pr_number"])

    if not _has_table("review_pull_requests"):
        op.create_table(
            "review_pull_requests",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("workspace_id", sa.Text(), nullable=False, server_default="default"),
            sa.Column("provider", sa.Text(), nullable=False),
            sa.Column("repo", sa.Text(), nullable=False),
            sa.Column("number", sa.Integer(), nullable=False),
            sa.Column("repo_slug", sa.Text(), nullable=True),
            sa.Column("title", sa.Text(), nullable=False, server_default=""),
            sa.Column("author", sa.Text(), nullable=True),
            sa.Column("url", sa.Text(), nullable=True),
            sa.Column("head_ref", sa.Text(), nullable=True),
            sa.Column("base_ref", sa.Text(), nullable=True),
            sa.Column("state", sa.Text(), nullable=False, server_default="open"),
            sa.Column("head_sha", sa.Text(), nullable=True),
            sa.Column("last_review_status", sa.Text(), nullable=True),
            sa.Column("last_run_id", sa.Text(), nullable=True),
            sa.Column("reviews_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("file_hashes", postgresql.JSONB(astext_type=sa.Text()),
                      nullable=True),
            sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint(
                "workspace_id", "provider", "repo", "number",
                name="uq_review_pull_request",
            ),
        )
        op.create_index("ix_review_pull_requests_ws_updated", "review_pull_requests",
                        ["workspace_id", "updated_at"])


def downgrade() -> None:
    if _has_table("review_pull_requests"):
        op.drop_index("ix_review_pull_requests_ws_updated",
                      table_name="review_pull_requests")
        op.drop_table("review_pull_requests")
    if _has_table("review_issues"):
        op.drop_index("ix_review_issues_pr", table_name="review_issues")
        op.drop_index("ix_review_issues_ws_seen", table_name="review_issues")
        op.drop_index("ix_review_issues_ws_status", table_name="review_issues")
        op.drop_table("review_issues")
    if _has_column("repo_review_policies", "comment_min_severity"):
        op.drop_column("repo_review_policies", "comment_min_severity")
    if _has_column("repo_review_policies", "ignore_globs"):
        op.drop_column("repo_review_policies", "ignore_globs")
