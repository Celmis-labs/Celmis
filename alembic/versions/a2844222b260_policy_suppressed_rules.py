"""policy_suppressed_rules — the repo layer gets the prefilter's deny-list

The review prefilter hides a fixed set of rule ids in code
(`ReviewSettings.suppressed_rules`): measured on the Martian bench with the
LLM veto off, six rules — tests.no-coverage, quality.todo, quality.typing,
quality.duplication, quality.maintainability, quality.magic_numbers — produced
6-7 false positives and not one true positive. A deny-list that lives only in
code and env is one an operator cannot change per repository, and the repo
policy already carries `disabled_agents` beside which this is the natural
shape.

Nullable with no server default, and no backfill, for the reason `disabled_agents`
is NOT NULL '[]' and this is not: there, "nothing disabled" and "inherit" are
the same empty list; here they are different answers. NULL is "inherit the
code default" — what every row that exists today should mean — and [] is "hide
nothing", a deliberate widening of what the review posts.

Additive, one head, reversible: the downgrade drops the column and the
prefilter falls back to the code default for every repository.

Revision ID: a2844222b260
Revises: b6d3f80a7e15
Create Date: 2026-08-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a2844222b260"
down_revision: str | Sequence[str] | None = "b6d3f80a7e15"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "repo_review_policies",
        sa.Column(
            "suppressed_rules",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("repo_review_policies", "suppressed_rules")
