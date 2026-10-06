"""feedback learning: what people said about findings, and the comments they said it on

1. `finding_signals` — append-only. One row per verdict, reply, reaction,
   resolved thread or outcome about a finding, keyed by the finding's
   PR-independent fingerprint, so a dismissal on one pull request is found on
   the next. The unique key makes every writer idempotent.

2. `posted_finding_comments` — the provider comment each finding was posted
   as, so a reply to a comment leads back to its finding. Never deleted when
   the provider comment is.

3. `learning_suppression` and `learning_excluded_reviewers` on
   `repo_review_policies` AND `workspace_review_defaults` — nullable, no
   server default, NULL = inherit (built-ins: shadow, none).

Revision ID: c1f6b8d0e256
Revises: e3b8d0f2a478
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "c1f6b8d0e256"
down_revision = "e3b8d0f2a478"
branch_labels = None
depends_on = None

_SIGNALS = "finding_signals"
_POSTED = "posted_finding_comments"
_POLICY = "repo_review_policies"
_DEFAULTS = "workspace_review_defaults"


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return False
    return column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    if not _has_table(_SIGNALS):
        op.create_table(
            "finding_signals",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("workspace_id", sa.Text(), nullable=False),
            sa.Column("repo_slug", sa.Text(), nullable=False),
            sa.Column("fingerprint", sa.Text(), nullable=False),
            sa.Column("file_path", sa.Text(), nullable=False, server_default=""),
            sa.Column("title", sa.Text(), nullable=False, server_default=""),
            sa.Column("body", sa.Text(), nullable=False, server_default=""),
            sa.Column("rule_id", sa.Text(), nullable=True),
            sa.Column("agent", sa.Text(), nullable=True),
            sa.Column("severity", sa.Text(), nullable=True),
            sa.Column("category", sa.Text(), nullable=True),
            sa.Column("signal", sa.Text(), nullable=False),
            sa.Column("source", sa.Text(), nullable=False),
            sa.Column("weight", sa.Float(), nullable=False, server_default="1"),
            sa.Column("reason", sa.Text(), nullable=False, server_default=""),
            sa.Column("actor", sa.Text(), nullable=False, server_default=""),
            sa.Column("actor_is_member", sa.Boolean(), nullable=False,
                      server_default=sa.text("false")),
            sa.Column("pr_provider", sa.Text(), nullable=False, server_default=""),
            sa.Column("pr_repo", sa.Text(), nullable=False, server_default=""),
            sa.Column("pr_number", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("comment_id", sa.Text(), nullable=True),
            sa.Column("run_id", sa.Text(), nullable=True),
            sa.Column("sha", sa.Text(), nullable=True),
            sa.Column("embedded", sa.Boolean(), nullable=False,
                      server_default=sa.text("false")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.UniqueConstraint(
                "workspace_id", "pr_provider", "pr_repo", "pr_number", "fingerprint",
                "signal", "source", "actor", name="uq_finding_signal"),
        )
        op.create_index("ix_finding_signals_fp", "finding_signals",
                        ["workspace_id", "repo_slug", "fingerprint"])
        op.create_index("ix_finding_signals_kind", "finding_signals",
                        ["workspace_id", "repo_slug", "signal", "created_at"])

    if not _has_table(_POSTED):
        op.create_table(
            "posted_finding_comments",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("workspace_id", sa.Text(), nullable=False),
            sa.Column("pr_provider", sa.Text(), nullable=False),
            sa.Column("pr_repo", sa.Text(), nullable=False),
            sa.Column("pr_number", sa.Integer(), nullable=False),
            sa.Column("comment_id", sa.Text(), nullable=False),
            sa.Column("run_id", sa.Text(), nullable=True),
            sa.Column("finding_key", sa.Text(), nullable=True),
            sa.Column("fingerprint", sa.Text(), nullable=False, server_default=""),
            sa.Column("repo_slug", sa.Text(), nullable=False, server_default=""),
            sa.Column("file_path", sa.Text(), nullable=False, server_default=""),
            sa.Column("line", sa.Integer(), nullable=True),
            sa.Column("title", sa.Text(), nullable=False, server_default=""),
            sa.Column("body_excerpt", sa.Text(), nullable=False, server_default=""),
            sa.Column("agent", sa.Text(), nullable=True),
            sa.Column("rule_id", sa.Text(), nullable=True),
            sa.Column("severity", sa.Text(), nullable=True),
            sa.Column("category", sa.Text(), nullable=True),
            sa.Column("evidence_kind", sa.Text(), nullable=True),
            sa.Column("sha", sa.Text(), nullable=True),
            # GitLab: a reply carries the discussion id, not the note id.
            sa.Column("thread_id", sa.Text(), nullable=True),
            sa.Column("posted_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.UniqueConstraint(
                "workspace_id", "pr_provider", "pr_repo", "pr_number", "comment_id",
                name="uq_posted_finding_comment"),
        )
        op.create_index("ix_posted_finding_comments_pr", "posted_finding_comments",
                        ["workspace_id", "pr_provider", "pr_repo", "pr_number"])

    # The tuples are spelled inline (not module constants) so the
    # migration-chain test can bind the loop variable to its literal names.
    for table in ("repo_review_policies", "workspace_review_defaults"):
        if not _has_column(table, "learning_suppression"):
            op.add_column(table, sa.Column("learning_suppression", sa.Text(), nullable=True))
        if not _has_column(table, "learning_excluded_reviewers"):
            op.add_column(table, sa.Column(
                "learning_excluded_reviewers", postgresql.JSONB(astext_type=sa.Text()),
                nullable=True))


def downgrade() -> None:
    for table in (_POLICY, _DEFAULTS):
        present = [
            c for c in ("learning_suppression", "learning_excluded_reviewers")
            if _has_column(table, c)
        ]
        if not present:
            continue
        with op.batch_alter_table(table) as batch:
            for column in present:
                batch.drop_column(column)
    if _has_table(_POSTED):
        op.drop_index("ix_posted_finding_comments_pr", table_name=_POSTED)
        op.drop_table(_POSTED)
    if _has_table(_SIGNALS):
        op.drop_index("ix_finding_signals_kind", table_name=_SIGNALS)
        op.drop_index("ix_finding_signals_fp", table_name=_SIGNALS)
        op.drop_table(_SIGNALS)
