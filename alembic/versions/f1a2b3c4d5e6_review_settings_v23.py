"""review settings 2.3.0: gates, provider actions, summary placement, messages

Kodus-parity review settings, at BOTH layers — `workspace_review_defaults`
(the workspace default) and `repo_review_policies` (the per-repo override):

- `enabled_agents` (JSON list) — opt-in agents switched on. Agents whose
  built-in is "off" (`business_logic`) run only when named here; a name in
  `disabled_agents` still wins. See
  `src.review.review_defaults.AGENT_PARTICIPATION_DEFAULTS`.
- booleans `run_on_drafts`, `approve_when_clean`,
  `request_changes_on_critical`, `status_feedback`,
  `committable_suggestions`, `apply_filters_to_rules`;
- closed-vocabulary text `summary_target`, `summary_on_new_commits`,
  `summary_existing_description`;
- free text `base_instruction`, `message_started`,
  `message_finished_header`.

`repo_review_policies` also gains `performance_model` and
`business_logic_model`: the per-repo model of the two 2.3.0 finders, a
column like the five `<agent>_model` columns before them.

Every column is NULLABLE with NO server default, and NULL is "inherit" —
the workspace default, then the built-in in
`src.review.review_defaults.BUILTIN_DEFAULTS`. No data is written: every
existing row reads NULL everywhere and so behaves exactly as before (drafts
skipped, no approval, summary as a comment, built-in messages, every agent
that ran before still running).

Idempotent both ways (each add / drop is guarded by an inspection), and
literal `op.add_column` calls over literal tuples only — the shape
tests/db/test_migration_chain.py can read.

Revision ID: f1a2b3c4d5e6
Revises: e4c8a1f7b2d9
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "f1a2b3c4d5e6"
down_revision = "e4c8a1f7b2d9"
branch_labels = None
depends_on = None

_POLICY = "repo_review_policies"
_DEFAULTS = "workspace_review_defaults"

_BOOL_COLUMNS = (
    "run_on_drafts", "approve_when_clean", "request_changes_on_critical",
    "status_feedback", "committable_suggestions", "apply_filters_to_rules",
)
_TEXT_COLUMNS = (
    "summary_target", "summary_on_new_commits", "summary_existing_description",
    "base_instruction", "message_started", "message_finished_header",
)
_MODEL_COLUMNS = ("performance_model", "business_logic_model")


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(table):
        return False
    return column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    # The tuples are spelled inline (not the module constants) so the
    # migration-chain test can bind each loop variable to its literal names.
    for table in ("repo_review_policies", "workspace_review_defaults"):
        if not _has_column(table, "enabled_agents"):
            op.add_column(table, sa.Column(
                "enabled_agents", postgresql.JSONB(astext_type=sa.Text()),
                nullable=True))
        for bool_column in (
            "run_on_drafts", "approve_when_clean", "request_changes_on_critical",
            "status_feedback", "committable_suggestions", "apply_filters_to_rules",
        ):
            if not _has_column(table, bool_column):
                op.add_column(table, sa.Column(bool_column, sa.Boolean(), nullable=True))
        for text_column in (
            "summary_target", "summary_on_new_commits", "summary_existing_description",
            "base_instruction", "message_started", "message_finished_header",
        ):
            if not _has_column(table, text_column):
                op.add_column(table, sa.Column(text_column, sa.Text(), nullable=True))
    for model_column in ("performance_model", "business_logic_model"):
        if not _has_column("repo_review_policies", model_column):
            op.add_column("repo_review_policies",
                          sa.Column(model_column, sa.Text(), nullable=True))


def downgrade() -> None:
    # batch_alter_table so SQLite (which cannot DROP COLUMN on older builds,
    # and never inside a table with JSON CHECKs) rebuilds the table; on
    # Postgres it is a plain ALTER TABLE … DROP COLUMN per column.
    for table, extra in ((_POLICY, _MODEL_COLUMNS), (_DEFAULTS, ())):
        present = [
            c for c in (*extra, *_TEXT_COLUMNS, *_BOOL_COLUMNS, "enabled_agents")
            if _has_column(table, c)
        ]
        if not present:
            continue
        with op.batch_alter_table(table) as batch:
            for column in present:
                batch.drop_column(column)
