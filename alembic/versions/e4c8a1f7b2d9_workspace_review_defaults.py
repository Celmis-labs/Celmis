"""workspace review defaults; repo policy fields learn to say "inherit"

1. `workspace_review_defaults` — one row per workspace, the layer between a
   repository's own policy and the install defaults: which agents take part
   (and whether the verifier's veto runs), the comment threshold, the inline
   cap, the summary switch and instructions, the "review started" comment,
   ignore globs, target branches and the prefilter's rule deny-list. Every
   column nullable; NULL = inherit the install default.

2. Four `repo_review_policies` columns could not say "inherit" until now:
   `disabled_agents` and `target_branches` were NOT NULL DEFAULT '[]', and
   `summary_enabled` / `started_comment_enabled` carried a server default TRUE
   that every row holds whether or not anybody chose it. They become plain
   nullable columns, and a stored value equal to the old built-in default
   ([] / TRUE) — indistinguishable from "never answered" — becomes NULL.

   No repository's behaviour changes on upgrade: the new table starts empty,
   so NULL resolves to exactly the built-in value it replaces. Every value
   that differs from the default (an agent switched off, a branch list, a
   summary turned off) stays stored, and so stays this repository's own
   answer above any workspace default set later.

Downgrade writes the built-in defaults back into the NULLs, restores NOT NULL
and the server defaults, and drops the table.

Revision ID: e4c8a1f7b2d9
Revises: d7a3e9c51b64
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "e4c8a1f7b2d9"
down_revision = "d7a3e9c51b64"
branch_labels = None
depends_on = None

_POLICY = "repo_review_policies"
_DEFAULTS = "workspace_review_defaults"
_LIST_COLUMNS = ("disabled_agents", "target_branches")
_SWITCH_COLUMNS = ("summary_enabled", "started_comment_enabled")


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if not _has_table(_DEFAULTS):
        op.create_table(
            "workspace_review_defaults",
            sa.Column("workspace_id", sa.Text(), primary_key=True),
            sa.Column("disabled_agents", postgresql.JSONB(astext_type=sa.Text()),
                      nullable=True),
            sa.Column("verifier_enabled", sa.Boolean(), nullable=True),
            sa.Column("comment_min_severity", sa.Text(), nullable=True),
            sa.Column("max_inline_comments", sa.Integer(), nullable=True),
            sa.Column("summary_enabled", sa.Boolean(), nullable=True),
            sa.Column("summary_instructions", sa.Text(), nullable=True),
            sa.Column("started_comment_enabled", sa.Boolean(), nullable=True),
            sa.Column("ignore_globs", postgresql.JSONB(astext_type=sa.Text()),
                      nullable=True),
            sa.Column("target_branches", postgresql.JSONB(astext_type=sa.Text()),
                      nullable=True),
            sa.Column("suppressed_rules", postgresql.JSONB(astext_type=sa.Text()),
                      nullable=True),
            sa.Column("updated_by", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
        )

    with op.batch_alter_table(_POLICY) as batch:
        for column in _LIST_COLUMNS:
            batch.alter_column(
                column, existing_type=postgresql.JSONB(astext_type=sa.Text()),
                nullable=True, server_default=None,
            )
        for column in _SWITCH_COLUMNS:
            batch.alter_column(
                column, existing_type=sa.Boolean(), existing_nullable=True,
                server_default=None,
            )

    bind = op.get_bind()
    for column in _LIST_COLUMNS:
        # CAST(... AS TEXT) reads the same on Postgres jsonb and SQLite JSON.
        bind.execute(sa.text(
            f"UPDATE {_POLICY} SET {column} = NULL "
            f"WHERE CAST({column} AS TEXT) = '[]'"
        ))
    for column in _SWITCH_COLUMNS:
        bind.execute(
            sa.text(f"UPDATE {_POLICY} SET {column} = NULL WHERE {column} = :on")
            .bindparams(on=True)
        )


def downgrade() -> None:
    bind = op.get_bind()
    for column in _LIST_COLUMNS:
        bind.execute(sa.text(
            f"UPDATE {_POLICY} SET {column} = '[]' WHERE {column} IS NULL"
        ))
    for column in _SWITCH_COLUMNS:
        bind.execute(
            sa.text(f"UPDATE {_POLICY} SET {column} = :on WHERE {column} IS NULL")
            .bindparams(on=True)
        )

    with op.batch_alter_table(_POLICY) as batch:
        for column in _LIST_COLUMNS:
            batch.alter_column(
                column, existing_type=postgresql.JSONB(astext_type=sa.Text()),
                nullable=False, server_default="[]",
            )
        for column in _SWITCH_COLUMNS:
            batch.alter_column(
                column, existing_type=sa.Boolean(), existing_nullable=True,
                server_default=sa.true(),
            )

    if _has_table(_DEFAULTS):
        op.drop_table(_DEFAULTS)
