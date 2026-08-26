"""incoming_alerts.language — the half of 8915d44 that never got a migration

The commit that taught the product to answer in the language of the question
added `language` to TWO models: automation_runs (migrated as e7b204c9f138)
and incoming_alerts (not migrated at all). SQLAlchemy therefore SELECTed a
column Postgres did not have, and GET /api/alerts answered 500 on every
request — the Incoming alerts panel has been dead since that deploy.

Nullable with no backfill on purpose: for rows written before the feature
existed, "we do not know what language this was" is the truth, and NULL says
it. An empty string would claim the question had no language.

Revision ID: d1a7f3e0c945
Revises: f1c6a90d4b73
Create Date: 2026-08-20
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision = "d1a7f3e0c945"
down_revision = "f1c6a90d4b73"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("incoming_alerts", sa.Column("language", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("incoming_alerts", "language")
