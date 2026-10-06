"""Jira task context: settings at both layers, the PR's task refs, a read cache

The business-logic agent used to check a pull request against its OWN text.
It now also reads the Jira task the PR names (src/review/task_context/), and
this migration is everything that needs storing for it:

- `repo_review_policies` AND `workspace_review_defaults` (NULL = inherit, the
  built-in is in `src.review.review_defaults.BUILTIN_DEFAULTS`):
    `task_context_enabled` (bool), `task_project_keys` (JSON list),
    `task_acceptance_field` (text, a `customfield_NNNNN` id),
    `task_include_comments` (int 0..10), `business_logic_auto` (text:
    off | when_task_found).
  and two columns the requirements check reads later (`requirements_check_mode`
  text, `task_urls_enabled` bool) — added here so that step needs no migration
  of its own.
- `review_pull_requests.task_refs` (JSON: the tasks the last review read) and
  `.requirements_check` (JSON: the last per-criterion verdicts).
- `task_context_cache`: one row per (workspace, Jira site, issue key) holding
  the curated issue the last read produced, so every worker shares one fetch.
  The Jira connection itself needs no table: it is a row of the encrypted
  credential store under provider `jira`.

Every column is NULLABLE with NO server default; nothing is written, so every
existing row behaves exactly as before. Idempotent both ways (each add / drop
is guarded by an inspection), and literal `op.add_column` calls only — the
shape tests/db/test_migration_chain.py can read.

Revision ID: d2a7c9e1f367
Revises: b0e5a7c9d145
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "d2a7c9e1f367"
down_revision = "b0e5a7c9d145"
branch_labels = None
depends_on = None

_POLICY = "repo_review_policies"
_DEFAULTS = "workspace_review_defaults"
_SETTING_COLUMNS = (
    "task_context_enabled", "task_project_keys", "task_acceptance_field",
    "task_include_comments", "business_logic_auto",
    "requirements_check_mode", "task_urls_enabled",
)


def _has_table(table: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table)


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return False
    return column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    for table in ("repo_review_policies", "workspace_review_defaults"):
        if not _has_column(table, "task_context_enabled"):
            op.add_column(table, sa.Column(
                "task_context_enabled", sa.Boolean(), nullable=True))
        if not _has_column(table, "task_project_keys"):
            op.add_column(table, sa.Column(
                "task_project_keys", postgresql.JSONB(astext_type=sa.Text()),
                nullable=True))
        if not _has_column(table, "task_acceptance_field"):
            op.add_column(table, sa.Column(
                "task_acceptance_field", sa.Text(), nullable=True))
        if not _has_column(table, "task_include_comments"):
            op.add_column(table, sa.Column(
                "task_include_comments", sa.Integer(), nullable=True))
        if not _has_column(table, "business_logic_auto"):
            op.add_column(table, sa.Column(
                "business_logic_auto", sa.Text(), nullable=True))
        if not _has_column(table, "requirements_check_mode"):
            op.add_column(table, sa.Column(
                "requirements_check_mode", sa.Text(), nullable=True))
        if not _has_column(table, "task_urls_enabled"):
            op.add_column(table, sa.Column(
                "task_urls_enabled", sa.Boolean(), nullable=True))

    if not _has_column("review_pull_requests", "task_refs"):
        op.add_column("review_pull_requests", sa.Column(
            "task_refs", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    if not _has_column("review_pull_requests", "requirements_check"):
        op.add_column("review_pull_requests", sa.Column(
            "requirements_check", postgresql.JSONB(astext_type=sa.Text()),
            nullable=True))

    if not _has_table("task_context_cache"):
        op.create_table(
            "task_context_cache",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("workspace_id", sa.Text(), nullable=False),
            sa.Column("site_host", sa.Text(), nullable=False),
            sa.Column("issue_key", sa.Text(), nullable=False),
            sa.Column("issue_updated", sa.Text(), nullable=True),
            sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()),
                      nullable=False),
            sa.Column("status", sa.Text(), nullable=False, server_default="ok"),
            sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.UniqueConstraint("workspace_id", "site_host", "issue_key",
                                name="uq_task_context_cache"),
        )
        op.create_index(
            "ix_task_context_cache_ws_fetched", "task_context_cache",
            ["workspace_id", "fetched_at"])


def downgrade() -> None:
    if _has_table("task_context_cache"):
        op.drop_index("ix_task_context_cache_ws_fetched",
                      table_name="task_context_cache")
        op.drop_table("task_context_cache")

    # batch_alter_table so SQLite rebuilds the table; on Postgres it is a
    # plain ALTER TABLE … DROP COLUMN per column.
    present = [c for c in ("task_refs", "requirements_check")
               if _has_column("review_pull_requests", c)]
    if present:
        with op.batch_alter_table("review_pull_requests") as batch:
            for column in present:
                batch.drop_column(column)

    for table in (_POLICY, _DEFAULTS):
        present = [c for c in _SETTING_COLUMNS if _has_column(table, c)]
        if not present:
            continue
        with op.batch_alter_table(table) as batch:
            for column in present:
                batch.drop_column(column)
