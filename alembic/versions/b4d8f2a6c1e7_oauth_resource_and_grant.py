"""OAuth: the resource a grant was made for, and the grant row behind a session

Browser sign-in for MCP clients needs two facts to survive from consent to
every later refresh, and neither had a column:

* ``resource`` (RFC 8707) on ``oauth_auth_codes`` and ``oauth_refresh_tokens``:
  the MCP endpoint the client asked the token for. The access token carries it
  as an extra audience, and a refresh cannot widen it.
* ``grant_id`` on ``oauth_refresh_tokens``: the ``mcp_tokens`` row that says
  what the session may do right now. Revoking that row ends the session.

Both nullable: a row made before this revision has neither and keeps working
exactly as it did.

Idempotent (a column that is already there is left alone); the downgrade drops
the columns with batch_alter_table so SQLite rebuilds the table.

Revision ID: b4d8f2a6c1e7
Revises: a3c5e7b91d24
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "b4d8f2a6c1e7"
down_revision = "a3c5e7b91d24"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return False
    return column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    if not _has_column("oauth_auth_codes", "resource"):
        op.add_column("oauth_auth_codes", sa.Column("resource", sa.Text(), nullable=True))
    if not _has_column("oauth_refresh_tokens", "resource"):
        op.add_column("oauth_refresh_tokens", sa.Column("resource", sa.Text(), nullable=True))
    if not _has_column("oauth_refresh_tokens", "grant_id"):
        op.add_column("oauth_refresh_tokens", sa.Column("grant_id", sa.Text(), nullable=True))


def downgrade() -> None:
    if _has_column("oauth_auth_codes", "resource"):
        with op.batch_alter_table("oauth_auth_codes") as batch:
            batch.drop_column("resource")
    for column in ("resource", "grant_id"):
        if _has_column("oauth_refresh_tokens", column):
            with op.batch_alter_table("oauth_refresh_tokens") as batch:
                batch.drop_column(column)
