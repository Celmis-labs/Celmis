"""access_requests — a general request for access to team workspaces

A signed-in person with no workspace but their personal one asks the
superadmin for access; the superadmin approves with one or more
(workspace, role) grants or rejects with a reason. See
src/api/routers/access_requests.py.

`user_id` is a string reference into the SQLite user store (src/users), like
every other user column in Postgres. One PENDING request per user is enforced
by a partial unique index.

Literal `op.create_table` only — see tests/db/test_migration_chain.py.

Revision ID: f6b1d3a8c240
Revises: e5a7c2f19d63
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "f6b1d3a8c240"
down_revision = "e5a7c2f19d63"
branch_labels = None
depends_on = None


def _has_table(table: str) -> bool:
    return table in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _has_table("access_requests"):
        return
    op.create_table(
        "access_requests",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column(
            "grants", postgresql.JSONB(astext_type=sa.Text()),
            nullable=False, server_default="[]",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_access_requests_user", "access_requests", ["user_id", "created_at"])
    op.create_index("ix_access_requests_status", "access_requests", ["status", "created_at"])
    op.create_index(
        "uq_access_requests_one_pending", "access_requests", ["user_id"],
        unique=True, postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    if not _has_table("access_requests"):
        return
    op.drop_index("uq_access_requests_one_pending", table_name="access_requests")
    op.drop_index("ix_access_requests_status", table_name="access_requests")
    op.drop_index("ix_access_requests_user", table_name="access_requests")
    op.drop_table("access_requests")
