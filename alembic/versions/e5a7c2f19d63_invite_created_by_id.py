"""workspace_invites.created_by_id — the issuer by id, not by email

Accepting an invite re-checks the issuer's right to grant its role, and the
issuer used to be found by `created_by`, an email. Emails change (the master
account follows CELMIS_MASTER_EMAIL), and after a change every pending invite
the issuer made was refused on accept. The id does not change. Older rows keep
NULL here and fall back to the email.

Literal `op.add_column` only — see tests/db/test_migration_chain.py.

Revision ID: e5a7c2f19d63
Revises: d8b2e6f41c90
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "e5a7c2f19d63"
down_revision = "d8b2e6f41c90"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    if not _has_column("workspace_invites", "created_by_id"):
        op.add_column("workspace_invites",
                      sa.Column("created_by_id", sa.Text(), nullable=True))


def downgrade() -> None:
    if _has_column("workspace_invites", "created_by_id"):
        op.drop_column("workspace_invites", "created_by_id")
