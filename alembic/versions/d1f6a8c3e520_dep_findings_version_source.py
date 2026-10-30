"""dep findings record where the version came from

A row's `current_version` used to be whatever the manifest scan produced, and
for a range (`>=4.2`, `^8`) that was the range's FLOOR presented as the
installed version. `version_source` says which it is: `lock`, `pin`,
`resolved` (resolved on the audit date, no lock file), `range` (only a floor;
nothing is installed that we know of) or `reported` (the auditing tool stated
the version).

Nullable: rows written before this column have no answer, which is not `lock`.

Revision ID: d1f6a8c3e520
Revises: c8d3a5f1b762
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "d1f6a8c3e520"
down_revision = "c8d3a5f1b762"
branch_labels = None
depends_on = None


def _has(column: str) -> bool:
    bind = op.get_bind()
    return column in {
        c["name"] for c in sa.inspect(bind).get_columns("dep_findings")
    }


def upgrade() -> None:
    if not _has("version_source"):
        op.add_column("dep_findings", sa.Column("version_source", sa.Text(), nullable=True))


def downgrade() -> None:
    if _has("version_source"):
        op.drop_column("dep_findings", "version_source")
