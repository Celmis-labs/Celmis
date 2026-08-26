"""policy_verifier_enabled — the false-positive veto becomes opt-in

The LLM veto shipped ON: a repository with no policy row ran it, because the
only way to switch it off was to name "verifier" in `disabled_agents`, and an
unconfigured repository names nothing. That is not the intended product. The
veto is a second model call over every finding in the review — the slowest
single call the pipeline makes — and whether it is worth its price is a
judgement about a repository's tolerance for noise, not something to charge
every installation by default.

Boolean and NULLABLE, not NOT NULL DEFAULT FALSE, and the distinction is the
one `suppressed_rules` draws next door: NULL is "inherit the install default"
(`REVIEW_VERIFIER_ENABLED`, itself False) and a stored value is "this
repository has decided". Collapsing them would make an operator who raises the
install default watch it apply to no repository, because every row would
already be holding an explicit answer nobody typed.

Not a column on `disabled_agents`' list, either. That list holds AGENTS, and
the veto is a stage that runs after them, over their combined output — the
orchestrator says so in three places, the parallel dispatcher never sees it,
and the bypass has to live in the orchestrator for exactly that reason.
Squeezing a stage into the agent deny-list is what made the default
un-invertible in the first place.

Additive, one head, reversible: the downgrade drops the column and every
repository falls back to the install default.

Revision ID: c8f1e2a70b34
Revises: a2844222b260
Create Date: 2026-08-24
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c8f1e2a70b34"
down_revision: str | Sequence[str] | None = "a2844222b260"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "repo_review_policies",
        sa.Column("verifier_enabled", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("repo_review_policies", "verifier_enabled")
