"""dep findings record whether this repository's own code names the package

A findings list is hundreds of rows long and every one reads the same: a
package, a version, an advisory. Nothing on the row said whether anything in
the repository actually mentions the package, so a direct dependency the
service imports on its hot path and a transitive package pulled in four levels
down by a build tool looked identical to whoever had to triage them.

`named_in_code` holds an import-position answer: the state, the reason, and up
to five `file:line` sites. THREE STATES, because the third is the honest one —
`imported`, `not_found`, and `unknown` for a package whose name does not
determine its module name. `beautifulsoup4` imports as `bs4`; reporting that as
"not imported" would be the silent zero this subsystem is built to refuse.

Nullable and JSONB rather than a string: an older row predates the scan and has
no answer, which is not the same as "not imported", and the sites belong with
the verdict rather than in a second column that can drift from it.

NOT reachability, and the column name says so on purpose. Reachability needs
the dependency's own source in the index, advisories that name the vulnerable
symbol, and a notion of where execution starts; this installation has none of
the three. A column called `reachable` would have been the same over-claim as
a manifest that does not hash itself proving a pack was not forged.

Revision ID: b7e3c19a5d40
Revises: a4d7f2c81e63
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "b7e3c19a5d40"
down_revision = "a4d7f2c81e63"
branch_labels = None
depends_on = None


def _has(column: str) -> bool:
    bind = op.get_bind()
    return column in {
        c["name"] for c in sa.inspect(bind).get_columns("dep_findings")
    }


def upgrade() -> None:
    # One literal call, not a loop: tests/db/test_migration_chain.py reads this
    # directory with `ast` and only sees literal `op.add_column("table",
    # sa.Column("name", ...))`. Anything dynamic makes that guard blind to the
    # failure it exists to catch — a model column with no migration behind it,
    # which is an UndefinedColumn on the first SELECT.
    if not _has("named_in_code"):
        op.add_column(
            "dep_findings",
            sa.Column("named_in_code", postgresql.JSONB(astext_type=sa.Text()),
                      nullable=True),
        )


def downgrade() -> None:
    if _has("named_in_code"):
        op.drop_column("dep_findings", "named_in_code")
