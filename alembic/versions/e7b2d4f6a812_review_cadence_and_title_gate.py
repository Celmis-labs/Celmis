"""review cadence, auto-pause and the title gate

Four more inheritable review settings, at BOTH layers (`repo_review_policies`
and `workspace_review_defaults`), NULL = inherit, no server default:

1. `review_cadence` (text) — `automatic` (the built-in), `auto_pause` or
   `manual`.
2. `auto_pause_pushes` (integer) — pushes inside the window that pause the
   automatic reviews of a PR (only read when the cadence is `auto_pause`).
3. `auto_pause_window_minutes` (integer) — the sliding window of that count.
4. `ignored_title_keywords` (JSON list) — a PR whose title contains one of
   them is not reviewed by an automatic trigger.

And the per-PR state those settings need on `review_pull_requests`:

* `last_reviewed_sha` / `last_reviewed_at` — the commit the last COMPLETE and
  POSTED review read; the baseline an incremental review starts from. Backfilled
  from `head_sha` for every PR whose last run was complete, so existing PRs
  have a baseline from their next push.
* `last_seen_sha` / `recent_pushes` (JSON list of ISO times) — what the push
  counter works from. A delivery for a head already seen is not a push.
* `review_paused` (NOT NULL, false) / `paused_reason` / `paused_at` /
  `paused_by` / `pause_notice_at` — whether automatic reviews of this PR wait,
  why, who said so, and when the one notice that says so was posted.

Idempotent: a column that is already there is left alone, so a database that
was stamped past this revision, or created from the models, upgrades cleanly.

Downgrade drops every column added here (batch_alter_table, so SQLite
rebuilds the table).

Revision ID: e7b2d4f6a812
Revises: d6a1c3e5f701
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "e7b2d4f6a812"
down_revision = "d6a1c3e5f701"
branch_labels = None
depends_on = None

_POLICY_COLUMNS = (
    "review_cadence", "auto_pause_pushes", "auto_pause_window_minutes",
    "ignored_title_keywords",
)
_PR_COLUMNS = (
    "last_reviewed_sha", "last_reviewed_at", "last_seen_sha", "recent_pushes",
    "review_paused", "paused_reason", "paused_at", "paused_by", "pause_notice_at",
)


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(table):
        return False
    return column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    # The table names are spelled inline so the migration-chain test can bind
    # the loop variable to its literal names.
    for table in ("repo_review_policies", "workspace_review_defaults"):
        if not _has_column(table, "review_cadence"):
            op.add_column(table, sa.Column("review_cadence", sa.Text(), nullable=True))
        if not _has_column(table, "auto_pause_pushes"):
            op.add_column(table, sa.Column("auto_pause_pushes", sa.Integer(), nullable=True))
        if not _has_column(table, "auto_pause_window_minutes"):
            op.add_column(
                table, sa.Column("auto_pause_window_minutes", sa.Integer(), nullable=True))
        if not _has_column(table, "ignored_title_keywords"):
            op.add_column(table, sa.Column(
                "ignored_title_keywords", postgresql.JSONB(astext_type=sa.Text()),
                nullable=True))

    if not _has_column("review_pull_requests", "last_reviewed_sha"):
        op.add_column("review_pull_requests", sa.Column("last_reviewed_sha", sa.Text(), nullable=True))
    if not _has_column("review_pull_requests", "last_reviewed_at"):
        op.add_column("review_pull_requests", sa.Column(
            "last_reviewed_at", sa.DateTime(timezone=True), nullable=True))
    if not _has_column("review_pull_requests", "last_seen_sha"):
        op.add_column("review_pull_requests", sa.Column("last_seen_sha", sa.Text(), nullable=True))
    if not _has_column("review_pull_requests", "recent_pushes"):
        op.add_column("review_pull_requests", sa.Column(
            "recent_pushes", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    if not _has_column("review_pull_requests", "review_paused"):
        op.add_column("review_pull_requests", sa.Column(
            "review_paused", sa.Boolean(), nullable=False, server_default=sa.false()))
    if not _has_column("review_pull_requests", "paused_reason"):
        op.add_column("review_pull_requests", sa.Column("paused_reason", sa.Text(), nullable=True))
    if not _has_column("review_pull_requests", "paused_at"):
        op.add_column("review_pull_requests", sa.Column(
            "paused_at", sa.DateTime(timezone=True), nullable=True))
    if not _has_column("review_pull_requests", "paused_by"):
        op.add_column("review_pull_requests", sa.Column("paused_by", sa.Text(), nullable=True))
    if not _has_column("review_pull_requests", "pause_notice_at"):
        op.add_column("review_pull_requests", sa.Column(
            "pause_notice_at", sa.DateTime(timezone=True), nullable=True))

    # A baseline for the PRs reviewed before this release: the head of the
    # last COMPLETE run. Partial / failed / skipped runs get none, so their
    # next review reads the whole PR.
    if (_has_column("review_pull_requests", "last_reviewed_sha")
            and _has_column("review_pull_requests", "head_sha")):
        op.execute(
            "UPDATE review_pull_requests SET last_reviewed_sha = head_sha "
            "WHERE last_review_status = 'complete' AND head_sha IS NOT NULL "
            "AND last_reviewed_sha IS NULL"
        )


def downgrade() -> None:
    for table in ("repo_review_policies", "workspace_review_defaults"):
        present = [c for c in _POLICY_COLUMNS if _has_column(table, c)]
        if not present:
            continue
        with op.batch_alter_table(table) as batch:
            for column in present:
                batch.drop_column(column)

    present = [c for c in _PR_COLUMNS if _has_column("review_pull_requests", c)]
    if present:
        with op.batch_alter_table("review_pull_requests") as batch:
            for column in present:
                batch.drop_column(column)
