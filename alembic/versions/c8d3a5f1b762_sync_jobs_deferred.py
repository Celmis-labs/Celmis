"""sync_jobs.status 'deferred': a review that waits for the model.

Four columns say why a job waits and since when, and the dedup index now also
covers 'deferred', so a new webhook for a pull request whose review is waiting
folds into the waiting job instead of creating a second one.

Revision ID: c8d3a5f1b762
Revises: 017a06ecb85d
Create Date: 2026-10-26
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "c8d3a5f1b762"
down_revision = "017a06ecb85d"
branch_labels = None
depends_on = None

_OLD_ACTIVE = "dedup_key IS NOT NULL AND status IN ('pending','running')"
_NEW_ACTIVE = "dedup_key IS NOT NULL AND status IN ('pending','running','deferred')"


def upgrade() -> None:
    op.add_column("sync_jobs", sa.Column(
        "deferred_since", sa.DateTime(timezone=True), nullable=True))
    op.add_column("sync_jobs", sa.Column("defer_reason", sa.Text(), nullable=True))
    op.add_column("sync_jobs", sa.Column("defer_code", sa.Text(), nullable=True))
    op.add_column("sync_jobs", sa.Column(
        "defer_checks", sa.Integer(), nullable=False, server_default="0"))
    op.drop_index("ix_sync_jobs_dedup", table_name="sync_jobs")
    op.create_index(
        "ix_sync_jobs_dedup", "sync_jobs", ["dedup_key"],
        postgresql_where=sa.text(_NEW_ACTIVE), unique=True,
    )
    op.create_index(
        "ix_sync_jobs_deferred", "sync_jobs", ["status", "next_run_at"],
        postgresql_where=sa.text("status = 'deferred'"),
    )


def downgrade() -> None:
    # The previous code knows no 'deferred'. A waiting job goes back to the
    # queue; one that would collide with a live job for the same PR is
    # cancelled instead, so the narrower unique index can be rebuilt.
    op.execute(
        "UPDATE sync_jobs SET status = 'cancelled', "
        "  last_error = 'deferred job dropped by downgrade' "
        "WHERE status = 'deferred' AND dedup_key IS NOT NULL AND EXISTS ("
        "  SELECT 1 FROM sync_jobs o WHERE o.dedup_key = sync_jobs.dedup_key "
        "  AND o.id <> sync_jobs.id AND o.status IN ('pending','running'))"
    )
    op.execute("UPDATE sync_jobs SET status = 'pending' WHERE status = 'deferred'")
    op.drop_index("ix_sync_jobs_deferred", table_name="sync_jobs")
    op.drop_index("ix_sync_jobs_dedup", table_name="sync_jobs")
    op.create_index(
        "ix_sync_jobs_dedup", "sync_jobs", ["dedup_key"],
        postgresql_where=sa.text(_OLD_ACTIVE), unique=True,
    )
    op.drop_column("sync_jobs", "defer_checks")
    op.drop_column("sync_jobs", "defer_code")
    op.drop_column("sync_jobs", "defer_reason")
    op.drop_column("sync_jobs", "deferred_since")
