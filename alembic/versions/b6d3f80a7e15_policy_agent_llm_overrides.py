"""policy_agent_llm_overrides — the repo layer gets the two settings it lacked

/admin/review-policies is the layer that WINS: a repo policy beats the
workspace `agents` entry, which beats the review profile, which beats
ReviewSettings. Until now it could set only the MODEL, through the five
`<agent>_model` columns — so the screen with the most authority showed the
least, and the output ceiling that failed the architect agent in 43% of runs
could be raised on /settings/llm and then silently outranked here.

ONE JSONB column rather than twelve more Text ones, shaped exactly like the
workspace `agents` blob — {"architect": {"max_output_tokens": 32768,
"reasoning": "high"}, …} — so both screens share one validator, one resolver
and one control. `model` is deliberately NOT a key in it: it stays in the five
columns `src/review/orchestrator.py` reads today, because two sources for one
field is the failure this project keeps hitting.

Nullable with no server default, and no backfill. Every row that already
exists reads back NULL, and NULL is exactly what "inherit" means at every
other layer of this chain; a `{}` default would say the same thing twice, in
a second dialect. Reversible: the downgrade drops the column, which loses the
overrides and nothing else — the models keep working off their own columns.

Revision ID: b6d3f80a7e15
Revises: d1a7f3e0c945
Create Date: 2026-08-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b6d3f80a7e15"
down_revision: str | Sequence[str] | None = "d1a7f3e0c945"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "repo_review_policies",
        sa.Column(
            "agent_llm_overrides",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("repo_review_policies", "agent_llm_overrides")
