"""The migration that gives the repo policy the prefilter's deny-list.

`tests/db/test_migration_chain.py` holds the whole chain as text — one head,
every parent resolving, the column added once and present for the model.
This one EXECUTES the revision for the two properties that are not visible
as text:

  - a `repo_review_policies` row that predates the column reads back as
    "inherit" (NULL), not as "hide nothing" ([]). Every policy in every
    installation is such a row, and the difference is whether the six
    measured-zero-TP rules keep being hidden on upgrade or all start posting;
  - it reverses, so a deploy that has to go back can.

Run against sqlite, like its sibling for `agent_llm_overrides`: the DDL is
the same shape either way, and a test that only runs where a Postgres happens
to be listening is not in the suite that gates the deploy.
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

REVISION = "a2844222b260"
COLUMN = "suppressed_rules"
MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic" / "versions" / f"{REVISION}_policy_suppressed_rules.py"
)


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw) -> str:  # pragma: no cover
    return "JSON"


def _migration():
    spec = importlib.util.spec_from_file_location(f"migration_{REVISION}", MIGRATION)
    assert spec and spec.loader, f"cannot load {MIGRATION}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _table_before_the_migration() -> sa.Table:
    """The model minus this column, so it cannot describe a table that never existed."""
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
    import datetime as dt

    e = sa.create_engine(f"sqlite:///{tmp_path}/celmis.db")
    legacy = _table_before_the_migration()
    legacy.create(e)
    now = dt.datetime.now(dt.UTC)
    with e.begin() as conn:
        conn.execute(legacy.insert().values(
            repo_slug="acme/api", workspace_id="default", enabled=True,
            prompt_template="rules", target_branches=["main"], folder_rules=[],
            agent_prompt_overrides={}, mcp_sources=[], disabled_agents=["verifier"],
            created_at=now, updated_at=now,
        ))
    try:
        yield e
    finally:
        e.dispose()


def _run(engine, direction: str) -> None:
    module = _migration()
    with engine.begin() as conn:
        module.op = Operations(MigrationContext.configure(conn))
        getattr(module, direction)()


def _columns(engine) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_columns("repo_review_policies")}


def test_it_chains_off_the_previous_head():
    module = _migration()
    assert module.revision == REVISION
    assert module.down_revision == "b6d3f80a7e15"


def test_a_row_that_predates_the_column_inherits_rather_than_hides_nothing(engine):
    _run(engine, "upgrade")

    assert COLUMN in _columns(engine)
    with Session(engine) as session:
        row = session.get(RepoReviewPolicy, "acme/api")
        assert row is not None
        assert row.suppressed_rules is None, (
            "an existing policy came out of the migration with [] — every "
            "repository would start posting the six rules measured at zero TP"
        )
        assert row.disabled_agents == ["verifier"], "additive: the row is otherwise untouched"


def test_an_empty_list_and_null_survive_as_different_answers(engine):
    _run(engine, "upgrade")

    with Session(engine) as session:
        row = session.get(RepoReviewPolicy, "acme/api")
        row.suppressed_rules = []
        session.commit()
    with Session(engine) as session:
        assert session.get(RepoReviewPolicy, "acme/api").suppressed_rules == []

    with Session(engine) as session:
        row = session.get(RepoReviewPolicy, "acme/api")
        row.suppressed_rules = ["quality.todo", "sec.cwe-862"]
        session.commit()
    with Session(engine) as session:
        assert session.get(RepoReviewPolicy, "acme/api").suppressed_rules == [
            "quality.todo", "sec.cwe-862",
        ]


def test_the_migration_reverses(engine):
    _run(engine, "upgrade")
    _run(engine, "downgrade")
    assert COLUMN not in _columns(engine)
    with engine.connect() as conn:
        assert conn.execute(sa.text(
            "SELECT count(*) FROM repo_review_policies"
        )).scalar_one() == 1
