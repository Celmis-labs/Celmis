"""The recheck state row of a branch is created in a transaction of its own.

On Postgres the row used to be inserted inside the pass's long transaction, so
a second pass for a branch's FIRST row blocked on the uncommitted insert (for
the whole git-and-model-call pass) instead of getting `busy` from
`FOR UPDATE SKIP LOCKED`. The lock itself is Postgres-only, so the order of
operations is pinned here with a stand-in session, and the SQLite path (no row
locks) is run for real.
"""

from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from src.db.models import ReviewIssueRecheckState
from src.review import issue_resolver

KEY = ("ws-acme", "github", "acme/api", "main")


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw) -> str:  # pragma: no cover
    return "JSON"


class _Result:
    def scalar_one_or_none(self):
        return "the-row"


class _PostgresSession:
    """Records what the pass's own transaction is asked to do."""

    def __init__(self) -> None:
        self.bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
        self.statements: list[str] = []

    def get_bind(self):
        return self.bind

    def execute(self, stmt):
        self.statements.append(str(stmt.compile(dialect=postgresql.dialect())).upper())
        return _Result()

    def flush(self):  # pragma: no cover - must not be reached on Postgres
        self.statements.append("FLUSH")


def test_on_postgres_the_row_is_created_in_its_own_transaction_before_the_lock(monkeypatch):
    seeded: list[tuple] = []
    monkeypatch.setattr(issue_resolver, "_seed_state_row",
                        lambda bind, key: seeded.append((bind, key)))
    session = _PostgresSession()

    row = issue_resolver._state_row(session, KEY)

    assert row == "the-row"
    assert seeded == [(session.bind, KEY)]
    assert len(session.statements) == 1, "the pass's transaction must only SELECT"
    assert session.statements[0].startswith("SELECT")
    assert "FOR UPDATE SKIP LOCKED" in session.statements[0]
    assert not any("INSERT" in s for s in session.statements)


def test_on_sqlite_the_row_is_created_and_found_again(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/c.db")
    ReviewIssueRecheckState.__table__.create(engine)
    with Session(engine) as s:
        first = issue_resolver._state_row(s, KEY)
        again = issue_resolver._state_row(s, KEY)
        assert first is not None and again is first
        assert (first.workspace_id, first.pr_repo, first.base_ref) == ("ws-acme", "acme/api", "main")
        assert first.llm_calls_total == 0
        s.commit()
