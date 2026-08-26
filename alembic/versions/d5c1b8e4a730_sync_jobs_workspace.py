"""sync_jobs.workspace_id — say which tenant a job belongs to

Revision ID: d5c1b8e4a730
Revises: c8e35b1a94d6
Create Date: 2026-08-19

The queue was the one workspace-shaped table with no workspace on it. The
tenant was buried in `payload` for the kinds that have one (index_repo_full,
generate_vault, regenerate_notes, review, deps_audit, automation_plan) and
absent for the kinds that do not (ownership_rebuild, cross_repo_materialize,
reindex_qdrant) — so the Jobs page could only ever be all-or-nothing, and
"all" means one tenant reading another's repository names.

The column is NULLABLE ON PURPOSE and, unlike every other workspace column
in this schema, has NO server_default of 'default'. A row with no tenant
must stay a row with no tenant: defaulting it would silently attribute
queue-wide maintenance to whoever happens to own the 'default' workspace.
NULL is read as global-admin-only by the API.

Backfill takes payload->>'workspace_id' where it is present and non-empty —
the same source the enqueue path now writes from, so historical rows and new
rows agree. Nothing is inferred for the kinds that never carried one.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "d5c1b8e4a730"
down_revision = "c8e35b1a94d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sync_jobs", sa.Column("workspace_id", sa.Text(), nullable=True))
    # jsonb ->> yields NULL when the key is absent, so a single guarded
    # UPDATE covers "has one" and leaves "has none" alone. NULLIF drops the
    # empty string, which is not a workspace id either.
    op.execute(
        "UPDATE sync_jobs "
        "SET workspace_id = NULLIF(payload->>'workspace_id', '') "
        "WHERE workspace_id IS NULL "
        "  AND jsonb_typeof(payload) = 'object' "
        "  AND NULLIF(payload->>'workspace_id', '') IS NOT NULL"
    )
    # The Jobs page reads "my workspace, newest first"; the worker still
    # reads "pending, oldest next_run_at first" off its own index.
    op.create_index(
        "ix_sync_jobs_workspace", "sync_jobs", ["workspace_id", "next_run_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_sync_jobs_workspace", table_name="sync_jobs")
    op.drop_column("sync_jobs", "workspace_id")
