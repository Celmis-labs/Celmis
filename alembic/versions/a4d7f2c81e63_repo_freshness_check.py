"""repo freshness check — when we last asked the remote, and what it said

`repo_index_state` recorded what was indexed and when. It could not answer the
question a user actually asks: *is this current?* A row whose
`last_indexed_at` is three days old means one of two very different things —
nobody has looked since, or we looked this morning and the branch has not
moved — and the difference is the whole answer.

Three columns, because "we checked and it is current", "we have not checked"
and "we tried and could not reach the remote" are three states, and collapsing
any two of them produces a screen that lies quietly.

WRITTEN OUT ONE CALL PER COLUMN, not looped. The first version built them from
a tuple, which is tidier and defeats
tests/db/test_migration_chain.py: that guard reads this directory with `ast`
and only sees LITERAL `op.add_column("table", sa.Column("name", ...))` calls —
by design, since anything dynamic would have to be guessed at. A loop here
makes the check silently blind to exactly the failure it exists to catch, a
model column with no migration behind it, which is a runtime `UndefinedColumn`
on the first SELECT.

Revision ID: a4d7f2c81e63
Revises: c8f1e2a70b34
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a4d7f2c81e63"
down_revision: str | Sequence[str] | None = "c8f1e2a70b34"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "repo_index_state"


def _has(name: str) -> bool:
    return name in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_TABLE)}


def upgrade() -> None:
    # When the remote was last asked. NULL means never — which is what every
    # existing row is, and it must not be backfilled to now(): claiming a
    # check that did not happen is the failure this table is being extended
    # to prevent.
    if not _has("last_checked_at"):
        op.add_column("repo_index_state",
                      sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True))
    # What the remote answered. Equal to last_indexed_sha → up to date.
    if not _has("last_remote_sha"):
        op.add_column("repo_index_state",
                      sa.Column("last_remote_sha", sa.Text(), nullable=True))
    # Why the last check failed, if it did. Separate from `last_error`, which
    # belongs to indexing: an index that succeeded and a check that cannot
    # reach the remote are unrelated conditions and share no remedy.
    if not _has("last_check_error"):
        op.add_column("repo_index_state",
                      sa.Column("last_check_error", sa.Text(), nullable=True))


def downgrade() -> None:
    if _has("last_check_error"):
        op.drop_column("repo_index_state", "last_check_error")
    if _has("last_remote_sha"):
        op.drop_column("repo_index_state", "last_remote_sha")
    if _has("last_checked_at"):
        op.drop_column("repo_index_state", "last_checked_at")
