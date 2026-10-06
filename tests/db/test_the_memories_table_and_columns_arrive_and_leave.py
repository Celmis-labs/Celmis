"""Migration f8c3e5a7b923: the memories table and the three learning settings
arrive and leave.

Executed against SQLite like its siblings:

  * it chains off a9d4f6b8c034;
  * the table arrives with the columns the model maps and the ORM can write a
    memory through it (server defaults included);
  * the three settings arrive nullable on BOTH policy tables;
  * the revision is idempotent and reverses.
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

REVISION = "f8c3e5a7b923"
MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic" / "versions" / f"{REVISION}_review_memories.py"
)
SETTINGS = ("memories_enabled", "knowledge_approval", "memory_trusted_commenters")
TABLES = ("repo_review_policies", "workspace_review_defaults")


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw) -> str:  # pragma: no cover
    return "JSON"


def _migration():
    spec = importlib.util.spec_from_file_location(f"migration_{REVISION}", MIGRATION)
    assert spec and spec.loader, f"cannot load {MIGRATION}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def engine(tmp_path):
    e = sa.create_engine(f"sqlite:///{tmp_path}/celmis.db")
    meta = sa.MetaData()
    sa.Table("repo_review_policies", meta,
             sa.Column("repo_slug", sa.Text, primary_key=True),
             sa.Column("workspace_id", sa.Text))
    sa.Table("workspace_review_defaults", meta,
             sa.Column("workspace_id", sa.Text, primary_key=True))
    meta.create_all(e)
    with e.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO repo_review_policies (repo_slug, workspace_id) VALUES ('a/b', 'ws-1')"))
        conn.execute(sa.text(
            "INSERT INTO workspace_review_defaults (workspace_id) VALUES ('ws-1')"))
    try:
        yield e
    finally:
        e.dispose()


def _run(engine, direction: str) -> None:
    module = _migration()
    with engine.begin() as conn:
        module.op = Operations(MigrationContext.configure(conn))
        getattr(module, direction)()


def _columns(engine, table: str) -> dict[str, dict]:
    return {c["name"]: c for c in sa.inspect(engine).get_columns(table)}


def test_it_chains_off_the_revision_before_it():
    module = _migration()
    assert module.revision == REVISION
    assert module.down_revision == "a9d4f6b8c034"


def test_the_table_arrives_with_what_the_model_maps(engine):
    from src.db.models import ReviewMemory

    _run(engine, "upgrade")
    inspector = sa.inspect(engine)
    assert inspector.has_table("review_memories")
    columns = {c["name"] for c in inspector.get_columns("review_memories")}
    assert {c.name for c in ReviewMemory.__table__.columns} == columns
    assert "ix_review_memories_scope" in {i["name"] for i in inspector.get_indexes("review_memories")}


@pytest.mark.parametrize("table", TABLES)
def test_the_settings_arrive_nullable_with_no_default_so_a_row_inherits(engine, table):
    _run(engine, "upgrade")
    columns = _columns(engine, table)
    for name in SETTINGS:
        assert name in columns, f"{table}.{name}"
        assert columns[name]["nullable"], f"{table}.{name}"
        assert columns[name]["default"] is None, f"{table}.{name}"
    with engine.connect() as conn:
        row = conn.execute(sa.text(f"SELECT {', '.join(SETTINGS)} FROM {table}")).one()
    assert tuple(row) == (None, None, None), "an existing row keeps inheriting"


def test_a_memory_round_trips_through_the_migrated_table(engine):
    from src.db.models import ReviewMemory

    _run(engine, "upgrade")
    with Session(engine) as s:
        s.add(ReviewMemory(workspace_id="ws-1", repo_slug=None, text="Money is integer cents."))
        s.commit()
    with engine.connect() as conn:
        row = conn.execute(sa.text(
            "SELECT id, status, origin, repo_slug, path_glob FROM review_memories")).one()
    assert row.id == 1
    assert (row.status, row.origin, row.repo_slug, row.path_glob) == (
        "pending", "manual", None, None), "nothing is active until somebody says so"


def test_it_is_idempotent_and_reverses(engine):
    _run(engine, "upgrade")
    _run(engine, "upgrade")
    _run(engine, "downgrade")
    inspector = sa.inspect(engine)
    assert not inspector.has_table("review_memories")
    for table in TABLES:
        assert inspector.has_table(table)
        assert not set(SETTINGS) & set(_columns(engine, table))
    _run(engine, "downgrade")  # nothing left to drop is not an error
