"""comment commands: the settings and the command ledger

Three more inheritable review settings, at BOTH layers (`repo_review_policies`
and `workspace_review_defaults`), NULL = inherit, no server default:

1. `commands_enabled` (boolean) — whether `@celmis ...` in a PR comment is
   answered at all.
2. `chat_enabled` (boolean) — whether a free-text question to the bot gets an
   answer (the commands themselves work either way).
3. `command_permission` (text) — who may command the bot: `repo_access`
   (the built-in), `participants` or `anyone`.

And `pr_command_events`, one row per comment the receiver accepted as a
command. The unique key (workspace, provider, repo, PR, comment id) is the
idempotency claim: a redelivery, a retry or an edit of a comment that was
already handled inserts nothing. The same rows are the rate limit and the
timeline on the pull-requests page.

Idempotent: a column or table that is already there is left alone, so a
database that was stamped past this revision, or created from the models,
upgrades cleanly.

Downgrade drops the table and every column added here (batch_alter_table, so
SQLite rebuilds the table).

Revision ID: a9d4f6b8c034
Revises: f9d4a6c8e134
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "a9d4f6b8c034"
down_revision = "f9d4a6c8e134"
branch_labels = None
depends_on = None

_POLICY_COLUMNS = ("commands_enabled", "chat_enabled", "command_permission")


def _has_table(table: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table)


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return False
    return column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    # The table names are spelled inline so the migration-chain test can bind
    # the loop variable to its literal names.
    for table in ("repo_review_policies", "workspace_review_defaults"):
        if not _has_column(table, "commands_enabled"):
            op.add_column(table, sa.Column("commands_enabled", sa.Boolean(), nullable=True))
        if not _has_column(table, "chat_enabled"):
            op.add_column(table, sa.Column("chat_enabled", sa.Boolean(), nullable=True))
        if not _has_column(table, "command_permission"):
            op.add_column(table, sa.Column("command_permission", sa.Text(), nullable=True))

    if not _has_table("pr_command_events"):
        op.create_table(
            "pr_command_events",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("workspace_id", sa.Text(), nullable=False, server_default="default"),
            sa.Column("provider", sa.Text(), nullable=False),
            sa.Column("repo", sa.Text(), nullable=False),
            sa.Column("pr_number", sa.Integer(), nullable=False),
            sa.Column("comment_id", sa.Text(), nullable=False),
            sa.Column("parent_id", sa.Text(), nullable=True),
            sa.Column("event_key", sa.Text(), nullable=True),
            sa.Column("command", sa.Text(), nullable=False),
            sa.Column("args", sa.Text(), nullable=True),
            sa.Column("force", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("actor_id", sa.Text(), nullable=True),
            sa.Column("actor_name", sa.Text(), nullable=True),
            sa.Column("status", sa.Text(), nullable=False, server_default="claimed"),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("reply_comment_id", sa.Text(), nullable=True),
            sa.Column("run_id", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      server_default=sa.func.now(), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint(
                "workspace_id", "provider", "repo", "pr_number", "comment_id",
                name="uq_pr_command_event"),
        )
        op.create_index(
            "ix_pr_command_events_pr", "pr_command_events",
            ["workspace_id", "provider", "repo", "pr_number", "created_at"])
        op.create_index(
            "ix_pr_command_events_actor", "pr_command_events",
            ["workspace_id", "provider", "actor_id", "created_at"])


def downgrade() -> None:
    if _has_table("pr_command_events"):
        op.drop_index("ix_pr_command_events_actor", table_name="pr_command_events")
        op.drop_index("ix_pr_command_events_pr", table_name="pr_command_events")
        op.drop_table("pr_command_events")

    for table in ("repo_review_policies", "workspace_review_defaults"):
        present = [c for c in _POLICY_COLUMNS if _has_column(table, c)]
        if not present:
            continue
        with op.batch_alter_table(table) as batch:
            for column in present:
                batch.drop_column(column)
