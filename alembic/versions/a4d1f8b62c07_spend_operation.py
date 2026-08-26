"""llm_spend.operation — which job inside a surface spent it

Revision ID: a4d1f8b62c07
Revises: f2a8c05d7e14
Create Date: 2026-08-18

`surface` says which part of the product spent the tokens. It could not say
which job inside that part, which is the difference between "the vault cost
$40" and "$34 of it was integration guides". The value was already a parameter
of every generate() call; it simply never reached the ledger.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "a4d1f8b62c07"
down_revision = "f2a8c05d7e14"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("llm_spend", sa.Column("operation", sa.Text(), nullable=True))
    # Both breakdowns the Usage page draws scan a workspace's window and group.
    # Without these they are sequential scans that get slower every day.
    op.create_index("ix_llm_spend_ws_repo", "llm_spend",
                    ["workspace_id", "repo_slug"])
    op.create_index("ix_llm_spend_ws_op", "llm_spend",
                    ["workspace_id", "operation"])


def downgrade() -> None:
    op.drop_index("ix_llm_spend_ws_op", table_name="llm_spend")
    op.drop_index("ix_llm_spend_ws_repo", table_name="llm_spend")
    op.drop_column("llm_spend", "operation")
