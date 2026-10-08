"""d4a7f1c8e3b2 executed both ways: the project file-scope columns and the
project-token table.

A database that predates them gets them; one created from the models (they are
already there) is left alone; a downgrade removes exactly what the upgrade made.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext

import tests.api.rbac_world  # noqa: F401 — registers JSONB → JSON for SQLite

VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"
REVISION = "d4a7f1c8e3b2"


def _module():
    [path] = VERSIONS.glob(f"{REVISION}_*.py")
    spec = importlib.util.spec_from_file_location(f"migration_{REVISION}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(engine, direction: str) -> None:
    module = _module()
    with engine.begin() as conn:
        module.op = Operations(MigrationContext.configure(conn))
        getattr(module, direction)()


@pytest.fixture
def engine(tmp_path):
    e = sa.create_engine(f"sqlite:///{tmp_path}/celmis.db")
    with e.begin() as conn:
        # The table as it was before this revision.
        conn.execute(sa.text(
            "CREATE TABLE project_repos (project_id TEXT, repo_slug TEXT, role TEXT, "
            "added_at TEXT, PRIMARY KEY (project_id, repo_slug))"))
    yield e
    e.dispose()


def _cols(engine, table):
    return {c["name"] for c in sa.inspect(engine).get_columns(table)}


def test_it_chains_onto_the_head_before_it():
    module = _module()
    assert module.revision == REVISION and module.down_revision == "b3e9d27f5a40"


def test_columns_and_table_arrive_and_leave(engine):
    _run(engine, "upgrade")
    assert {"include_globs", "exclude_globs"} <= _cols(engine, "project_repos")
    assert "mcp_project_tokens" in sa.inspect(engine).get_table_names()
    assert "ix_mcp_project_tokens_project" in {
        i["name"] for i in sa.inspect(engine).get_indexes("mcp_project_tokens")}
    _run(engine, "downgrade")
    assert not {"include_globs", "exclude_globs"} & _cols(engine, "project_repos")
    assert "mcp_project_tokens" not in sa.inspect(engine).get_table_names()


def test_the_table_holds_the_columns_the_model_reads(engine):
    from src.db.models import McpProjectToken

    _run(engine, "upgrade")
    assert {c.name for c in McpProjectToken.__table__.columns} <= _cols(
        engine, "mcp_project_tokens")


def test_a_second_upgrade_and_a_model_made_database_are_left_alone(engine, tmp_path):
    _run(engine, "upgrade")
    _run(engine, "upgrade")
    from src.db.models import Base

    other = sa.create_engine(f"sqlite:///{tmp_path}/models.db")
    Base.metadata.create_all(other)
    _run(other, "upgrade")
    other.dispose()


def test_a_downgrade_with_nothing_to_remove_does_not_raise(engine):
    _run(engine, "downgrade")
