"""Migration f9d4a6c8e134: the `review_scope` setting.

One nullable text column on both policy tables (NULL = inherit, no server
default), an idempotent upgrade that leaves a database created from the models
alone, and a downgrade that drops exactly that column and keeps the rows.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from src.db.models import RepoReviewPolicy, WorkspaceReviewDefaults

REVISION = "f9d4a6c8e134"
MIGRATION = (Path(__file__).resolve().parents[2] / "alembic" / "versions"
             / f"{REVISION}_review_scope_setting.py")
TABLES = {"repo_review_policies": RepoReviewPolicy,
          "workspace_review_defaults": WorkspaceReviewDefaults}


def _migration():
    spec = importlib.util.spec_from_file_location("m_f9d4a6c8e134", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _legacy(table: str) -> list[sa.Column]:
    """The table as e7b2d4f6a812 left it: the model's columns minus the new one."""
    return [
        sa.Column(c.name, sa.JSON() if "JSON" in type(c.type).__name__.upper() else c.type,
                  primary_key=c.primary_key, nullable=c.nullable,
                  server_default=sa.text("''") if c.name == "prompt_template" else None)
        for c in TABLES[table].__table__.columns if c.name != "review_scope"
    ]


@pytest.fixture
def engine(tmp_path):
    e = sa.create_engine(f"sqlite:///{tmp_path}/celmis.db")
    md = sa.MetaData()
    for table in TABLES:
        sa.Table(table, md, *_legacy(table))
    md.create_all(e)
    yield e
    e.dispose()


def _run(engine, direction: str) -> None:
    module = _migration()
    with engine.begin() as conn:
        module.op = Operations(MigrationContext.configure(conn))
        getattr(module, direction)()


def _columns(engine, table: str) -> dict[str, dict]:
    return {c["name"]: c for c in sa.inspect(engine).get_columns(table)}


def test_it_chains_off_the_cadence_revision():
    module = _migration()

    assert (module.revision, module.down_revision) == (REVISION, "e7b2d4f6a812")


def test_the_column_arrives_nullable_on_both_tables(engine):
    _run(engine, "upgrade")

    for table in TABLES:
        columns = _columns(engine, table)
        assert "review_scope" in columns and columns["review_scope"]["nullable"], table
        assert columns["review_scope"]["default"] is None, f"{table}: NULL means inherit"


def test_upgrading_twice_changes_nothing(engine):
    _run(engine, "upgrade")
    _run(engine, "upgrade")

    assert "review_scope" in _columns(engine, "repo_review_policies")


def test_a_database_that_already_has_the_column_upgrades_cleanly(tmp_path):
    e = sa.create_engine(f"sqlite:///{tmp_path}/from_models.db")
    md = sa.MetaData()
    for table, model in TABLES.items():
        sa.Table(table, md, *[
            sa.Column(c.name, sa.JSON() if "JSON" in type(c.type).__name__.upper() else c.type,
                      primary_key=c.primary_key, nullable=c.nullable,
                      server_default=sa.text("''") if c.name == "prompt_template" else None)
            for c in model.__table__.columns])
    md.create_all(e)

    _run(e, "upgrade")

    assert "review_scope" in _columns(e, "workspace_review_defaults")
    e.dispose()


def test_downgrade_drops_only_that_column(engine):
    _run(engine, "upgrade")
    before = {t: set(_columns(engine, t)) - {"review_scope"} for t in TABLES}

    _run(engine, "downgrade")

    for table in TABLES:
        assert set(_columns(engine, table)) == before[table], table
    _run(engine, "downgrade")  # again: nothing left to drop
    _run(engine, "upgrade")
    assert "review_scope" in _columns(engine, "repo_review_policies")
