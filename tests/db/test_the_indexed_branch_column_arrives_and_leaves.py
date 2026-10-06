"""repo_index_state.indexed_branch: the migration that stores the branch of an index.

Executed against sqlite (the DDL is the same shape on Postgres) for the two
properties that are not visible as text: a row written before the column
reads back as "unknown" (NULL) rather than as a branch name nobody recorded,
and the revision reverses and re-applies cleanly.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext

from src.db.models import RepoIndexState

REVISION = "b3e9d27f5a40"
COLUMN = "indexed_branch"
MIGRATION = (Path(__file__).resolve().parents[2] / "alembic" / "versions"
             / f"{REVISION}_repo_index_state_indexed_branch.py")


def _migration():
    spec = importlib.util.spec_from_file_location(f"migration_{REVISION}", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(engine, direction: str) -> None:
    module = _migration()
    with engine.begin() as conn:
        module.op = Operations(MigrationContext.configure(conn))
        getattr(module, direction)()


def _columns(engine) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_columns("repo_index_state")}


@pytest.fixture
def engine(tmp_path):
    e = sa.create_engine(f"sqlite:///{tmp_path}/celmis.db")
    legacy = sa.Table(
        "repo_index_state", sa.MetaData(),
        *[sa.Column(c.name, c.type, primary_key=c.primary_key, nullable=c.nullable)
          for c in RepoIndexState.__table__.columns if c.name != COLUMN])
    legacy.create(e)
    with e.begin() as conn:
        conn.execute(legacy.insert().values(repo_slug="github_acme-shop",
                                            last_indexed_sha="a" * 40,
                                            last_incremental_files=0))
    yield e
    e.dispose()


def test_it_chains_off_a_single_parent():
    module = _migration()
    assert module.revision == REVISION and module.down_revision


def test_a_row_that_predates_the_column_reads_back_as_unknown(engine):
    _run(engine, "upgrade")
    assert COLUMN in _columns(engine)
    with engine.connect() as conn:
        row = conn.execute(sa.text(
            "select last_indexed_sha, indexed_branch from repo_index_state")).one()
    assert row[0] == "a" * 40 and row[1] is None


def test_upgrading_twice_is_harmless(engine):
    _run(engine, "upgrade")
    _run(engine, "upgrade")
    assert COLUMN in _columns(engine)


def test_downgrade_removes_the_column_and_keeps_the_rows(engine):
    _run(engine, "upgrade")
    _run(engine, "downgrade")
    assert COLUMN not in _columns(engine)
    with engine.connect() as conn:
        assert conn.execute(sa.text("select count(*) from repo_index_state")).scalar() == 1


def test_the_model_declares_the_column_the_migration_adds():
    assert COLUMN in RepoIndexState.__table__.columns
