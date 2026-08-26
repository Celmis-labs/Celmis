"""The one migration that gives the repo policy its ceiling and its reasoning.

`tests/db/test_migration_chain.py` reads the chain as text — one head, no
dangling parent, no column added twice, nothing in a model the chain never
creates. Those are the failures that took production to 502, and they are all
statically visible.

This one EXECUTES the revision, because two of its properties are not:

  - a `repo_review_policies` row that predates the column has to read back as
    "inherit". Every policy in every existing installation is such a row, and
    the alternative — a value nobody chose arriving as an override at the layer
    that outranks every other — would silently reshape reviews on upgrade;
  - it has to reverse. `alembic downgrade` is the rollback path for a deploy
    that has to go back, and a downgrade that raises leaves the operator
    holding a database that matches neither release.

One head, a parent that resolves, a column added exactly once: all three
stay in `test_migration_chain.py`, which holds the WHOLE chain at once and
would catch this revision breaking any of them. Re-asserting them for one
file would be a second, narrower copy of a test that already passes.

Run against sqlite: the shapes involved (add a nullable column, drop it) are
the same DDL either way, and the alternative is a test that only runs where a
Postgres happens to be listening — which is to say, not in the suite that
gates the deploy.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from src.db.models import RepoReviewPolicy

REVISION = "b6d3f80a7e15"
COLUMN = "agent_llm_overrides"
MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic" / "versions" / f"{REVISION}_policy_agent_llm_overrides.py"
)


# JSONB rendered as sqlite's JSON — a test-side shim. The DDL that reaches a
# real database is still exactly what Alembic emits.
@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw) -> str:  # pragma: no cover
    return "JSON"


def _migration():
    """The revision module, loaded from the file Alembic itself would run."""
    spec = importlib.util.spec_from_file_location(f"migration_{REVISION}", MIGRATION)
    assert spec and spec.loader, f"cannot load {MIGRATION}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _table_before_the_migration() -> sa.Table:
    """`repo_review_policies` as it stood one revision ago.

    Derived from the model minus this column rather than typed out, so it
    cannot drift into describing a table that never existed. Server defaults
    are dropped deliberately — `enabled DEFAULT true()` is Postgres SQL that
    sqlite cannot evaluate, and defaults are not what is under test.
    """
    return sa.Table(
        RepoReviewPolicy.__tablename__,
        sa.MetaData(),
        *[
            sa.Column(c.name, c.type, primary_key=c.primary_key, nullable=c.nullable)
            for c in RepoReviewPolicy.__table__.columns
            if c.name != COLUMN
        ],
    )


@pytest.fixture
def engine(tmp_path):
    """A database holding one policy row written before the column existed."""
    import datetime as dt

    e = sa.create_engine(f"sqlite:///{tmp_path}/celmis.db")
    legacy = _table_before_the_migration()
    legacy.create(e)
    now = dt.datetime.now(dt.UTC)
    with e.begin() as conn:
        conn.execute(legacy.insert().values(
            repo_slug="acme/api", workspace_id="default", enabled=True,
            prompt_template="rules", target_branches=["main"], folder_rules=[],
            architect_model="gpt-4o", agent_prompt_overrides={},
            mcp_sources=[], disabled_agents=["quality"],
            created_at=now, updated_at=now,
        ))
    try:
        yield e
    finally:
        e.dispose()


def _run(engine, direction: str) -> None:
    """Drive the revision's `upgrade()` / `downgrade()` for real.

    The module reaches for `alembic.op`, the proxy Alembic binds while a
    migration runs; standing an `Operations` in its place is that binding,
    done by hand.
    """
    module = _migration()
    with engine.begin() as conn:
        module.op = Operations(MigrationContext.configure(conn))
        getattr(module, direction)()


def _columns(engine) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_columns("repo_review_policies")}


def test_the_column_arrives_and_the_rows_that_predate_it_read_as_inherit(engine):
    _run(engine, "upgrade")

    assert COLUMN in _columns(engine)
    with Session(engine) as session:
        row = session.get(RepoReviewPolicy, "acme/api")
        assert row is not None, (
            "the model can no longer read the migrated table — the two "
            "disagree about some other column"
        )
        assert not row.agent_llm_overrides, (
            "an existing policy came out of the migration carrying an "
            "override nobody chose, at the layer that outranks every other"
        )
        # What the row already said is untouched: this is an additive change.
        assert row.architect_model == "gpt-4o"
        assert row.disabled_agents == ["quality"]


def test_a_value_written_after_the_migration_round_trips_as_a_map(engine):
    """It is a JSON column, not a string of JSON: the resolver indexes into it
    per agent and would read a string one character at a time."""
    _run(engine, "upgrade")

    with Session(engine) as session:
        row = session.get(RepoReviewPolicy, "acme/api")
        row.agent_llm_overrides = {
            "architect": {"max_output_tokens": 40000, "reasoning": "high"},
        }
        session.commit()

    with Session(engine) as session:
        stored = session.get(RepoReviewPolicy, "acme/api").agent_llm_overrides
        assert stored == {
            "architect": {"max_output_tokens": 40000, "reasoning": "high"},
        }


def test_the_migration_reverses(engine):
    """The rollback path for a deploy that has to go back."""
    _run(engine, "upgrade")
    with Session(engine) as session:
        row = session.get(RepoReviewPolicy, "acme/api")
        row.agent_llm_overrides = {"architect": {"max_output_tokens": 40000}}
        session.commit()

    _run(engine, "downgrade")

    assert COLUMN not in _columns(engine)
    # The policy the release was for still works off its own columns.
    legacy = _table_before_the_migration()
    with engine.begin() as conn:
        row = conn.execute(sa.select(legacy)).mappings().one()
    assert row["architect_model"] == "gpt-4o"
    assert row["prompt_template"] == "rules"
