"""stage18_queue_and_index_state

Revision ID: b5f83a1c9d02
Revises: e7a92b4c8f01
Create Date: 2026-07-16 12:00:00.000000

Stage 18 — durable job queue + repo indexing state.

Tables:
  * sync_jobs — Postgres-backed job queue (SKIP LOCKED pattern).
    Backing store for review dispatch, index sync, ownership rebuild,
    etc. Survives API downtime, retries with exponential backoff, dead-
    letters on max attempts.
  * repo_index_state — one row per repo, records last_indexed_sha so
    the next index run can be incremental (git diff old..new).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b5f83a1c9d02"
down_revision: str | Sequence[str] | None = "e7a92b4c8f01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sync_jobs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        # kind: 'review' | 'index_repo' | 'ownership_rebuild'
        #     | 'cross_repo_materialize' | 'reindex_qdrant'
        sa.Column("dedup_key", sa.Text(), nullable=True),
        # Optional idempotency key — enqueue is a no-op if a pending
        # row with the same dedup_key already exists. E.g.
        # "review:gitlab:owner/repo#42:sha_abc" ensures a webhook that
        # fires twice for the same commit only runs one review.
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        # pending | running | completed | failed | dead
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("next_run_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("enqueued_by", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Composite index: worker's dequeue query filters on
    # status='pending' AND next_run_at <= now, orders by next_run_at.
    op.create_index(
        "ix_sync_jobs_pending", "sync_jobs",
        ["status", "next_run_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_sync_jobs_dedup", "sync_jobs",
        ["dedup_key"],
        postgresql_where=sa.text("dedup_key IS NOT NULL AND status IN ('pending','running')"),
        unique=True,
    )

    op.create_table(
        "repo_index_state",
        sa.Column("repo_slug", sa.Text(), primary_key=True),
        sa.Column("last_indexed_sha", sa.Text(), nullable=True),
        sa.Column("last_indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_full_rebuild_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_incremental_files", sa.Integer(),
                  nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("repo_index_state")
    op.drop_index("ix_sync_jobs_dedup", table_name="sync_jobs")
    op.drop_index("ix_sync_jobs_pending", table_name="sync_jobs")
    op.drop_table("sync_jobs")
