"""review rules: a workspace/repository rules library, and the jobs that fill it

1. `review_rules` — one row per review rule. `repo_slug` NULL is a rule for
   every repository of the workspace; a slug scopes it to that repository,
   where a rule with the same title replaces the workspace one. `status` is
   active | pending | rejected: only active rules reach a review, and every
   rule a machine wrote (generated, imported, proposed by the agent) arrives
   pending, for a person to approve. `origin` says where it came from
   (manual | library | generated | imported | agent) and `source_ref` names
   the library entry, the file or the job it was read from.

2. `review_rule_jobs` — "Generate rules" and "Import from repo files" run in
   the background; this is the row the page polls for their progress.

`repo_review_policies.folder_rules` is NOT converted. Those rules keep
rendering exactly as they did, from the policy row the policy page edits:
copying them here would leave two editable homes for one rule, and the
policy PUT — which replaces the list wholesale — would silently undo every
edit made on the new page. The reviews read both; a downgrade therefore loses
nothing but the new tables.

Revision ID: a7b8c9d0e1f2
Revises: f1a2b3c4d5e6
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "a7b8c9d0e1f2"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None

_RULES = "review_rules"
_JOBS = "review_rule_jobs"


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if not _has_table(_RULES):
        op.create_table(
            "review_rules",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("workspace_id", sa.Text(), nullable=False),
            sa.Column("repo_slug", sa.Text(), nullable=True),
            sa.Column("title", sa.Text(), nullable=False),
            sa.Column("instructions", sa.Text(), nullable=False),
            sa.Column("path_glob", sa.Text(), nullable=True),
            sa.Column("severity", sa.Text(), nullable=False,
                      server_default="warning"),
            sa.Column("agents", postgresql.JSONB(astext_type=sa.Text()),
                      nullable=False, server_default="[]"),
            sa.Column("examples_good", sa.Text(), nullable=True),
            sa.Column("examples_bad", sa.Text(), nullable=True),
            sa.Column("rationale", sa.Text(), nullable=True),
            sa.Column("status", sa.Text(), nullable=False,
                      server_default="pending"),
            sa.Column("origin", sa.Text(), nullable=False,
                      server_default="manual"),
            sa.Column("source_ref", sa.Text(), nullable=True),
            sa.Column("created_by", sa.Text(), nullable=True),
            sa.Column("updated_by", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
        )
        op.create_index("ix_review_rules_scope", "review_rules",
                        ["workspace_id", "repo_slug", "status"])

    if not _has_table(_JOBS):
        op.create_table(
            "review_rule_jobs",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("workspace_id", sa.Text(), nullable=False),
            sa.Column("repo_slug", sa.Text(), nullable=False),
            sa.Column("kind", sa.Text(), nullable=False),
            sa.Column("status", sa.Text(), nullable=False,
                      server_default="queued"),
            sa.Column("progress", sa.Text(), nullable=True),
            sa.Column("result", postgresql.JSONB(astext_type=sa.Text()),
                      nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("created_by", sa.Text(), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
        )
        op.create_index("ix_review_rule_jobs_ws", "review_rule_jobs",
                        ["workspace_id", "created_at"])


def downgrade() -> None:
    if _has_table(_JOBS):
        op.drop_index("ix_review_rule_jobs_ws", table_name="review_rule_jobs")
        op.drop_table(_JOBS)
    if _has_table(_RULES):
        op.drop_index("ix_review_rules_scope", table_name="review_rules")
        op.drop_table(_RULES)
