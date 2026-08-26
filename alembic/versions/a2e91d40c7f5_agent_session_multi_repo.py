"""agent sessions: several repos per session

A session used to be one repo. It is now a set — picked either directly or
through a Q&A project, which already groups repos for exactly this reason.

Shape: a JSONB array on the session row plus the originating project id, NOT a
join table. The set is written once at creation and never edited, it is read
whole on every session render (list + detail + the runner's clone loop), and it
is capped at a handful of entries — so a child table would buy a second write,
a join on every read and referential machinery for a value that behaves like a
scalar. Nothing needs to ask "which sessions touched repo X" today; if that ever
lands, `repo_slugs` takes a GIN index and answers it with @>.

`repo_slug` stays exactly as it was and keeps carrying the FIRST repo: the spend
ledger, the push notification and the session list all read it, and a session
created by an older client still sends only that field.

No backfill. Old rows get `[]` and are read through `AgentSession.slugs`, which
falls back to `[repo_slug]` — correct for every existing row, and also for rows
an old API replica writes while the deploy is halfway through.

Revision ID: a2e91d40c7f5
Revises: f8b2d41e7c96
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a2e91d40c7f5"
down_revision: str | Sequence[str] | None = "f8b2d41e7c96"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # server_default, not a Python-side default: the runner loads existing rows
    # and a NULL here would have to be defended against at every read site.
    op.add_column(
        "agent_sessions",
        sa.Column(
            "repo_slugs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
    )
    # Nullable and un-FK'd: a session outlives the project it was started from,
    # and losing the transcript because someone tidied up projects would be a
    # worse bug than a dangling id.
    op.add_column(
        "agent_sessions",
        sa.Column("project_id", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    # Only the extra columns go. `repo_slug` was never touched, so every row
    # downgrades to its first repo — which is what the pre-multi-repo code
    # reads anyway. Sessions that covered several repos lose the other slugs;
    # that is the honest cost of the downgrade, not something to fake.
    op.drop_column("agent_sessions", "project_id")
    op.drop_column("agent_sessions", "repo_slugs")
