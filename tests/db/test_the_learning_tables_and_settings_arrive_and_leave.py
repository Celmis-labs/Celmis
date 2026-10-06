"""Migration c1f6b8d0e256: the signals tables and the two learning settings
arrive and leave.

Executed against SQLite like its siblings:

  * it chains off e3b8d0f2a478 and is the one head;
  * both tables arrive with the columns the models map, and the unique keys
    that make every writer idempotent;
  * the two settings arrive nullable, with no default, on BOTH policy tables;
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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

REVISION = "c1f6b8d0e256"
VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"
MIGRATION = VERSIONS / f"{REVISION}_learning_signals.py"
SETTINGS = ("learning_suppression", "learning_excluded_reviewers")
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
    assert module.down_revision == "e3b8d0f2a478"


def test_nothing_else_branches_off_the_same_revision():
    """Two children of one revision are two heads, and `alembic upgrade head`
    refuses to run."""
    children = []
    for path in VERSIONS.glob("*.py"):
        text = path.read_text()
        if 'down_revision = "e3b8d0f2a478"' in text:
            children.append(path.name)
    assert children == [MIGRATION.name], children


def test_both_tables_arrive_with_what_the_models_map(engine):
    from src.db.models import FindingSignal, PostedFindingComment

    _run(engine, "upgrade")
    inspector = sa.inspect(engine)
    for model in (FindingSignal, PostedFindingComment):
        name = model.__tablename__
        assert inspector.has_table(name), name
        assert {c.name for c in model.__table__.columns} == {
            c["name"] for c in inspector.get_columns(name)}, name
    assert {"ix_finding_signals_fp", "ix_finding_signals_kind"} <= {
        i["name"] for i in inspector.get_indexes("finding_signals")}


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
    assert tuple(row) == (None, None), "an existing row keeps inheriting"


def test_the_same_person_saying_the_same_thing_twice_is_one_row(engine):
    from src.db.models import FindingSignal

    _run(engine, "upgrade")

    def signal() -> FindingSignal:
        return FindingSignal(
            workspace_id="ws-1", repo_slug="r", fingerprint="f" * 64, signal="dismissed",
            source="reply", actor="jane", pr_provider="github", pr_repo="acme/shop",
            pr_number=7)

    with Session(engine) as s:
        s.add(signal())
        s.commit()
    with Session(engine) as s:
        s.add(signal())
        with pytest.raises(IntegrityError):
            s.commit()


def test_a_provider_comment_maps_to_one_finding(engine):
    from src.db.models import PostedFindingComment

    _run(engine, "upgrade")

    def posted() -> PostedFindingComment:
        return PostedFindingComment(
            workspace_id="ws-1", pr_provider="github", pr_repo="acme/shop", pr_number=7,
            comment_id="c1", fingerprint="f" * 64, repo_slug="r")

    with Session(engine) as s:
        s.add(posted())
        s.commit()
    with Session(engine) as s:
        s.add(posted())
        with pytest.raises(IntegrityError):
            s.commit()


def test_it_is_idempotent_and_reverses(engine):
    _run(engine, "upgrade")
    _run(engine, "upgrade")
    _run(engine, "downgrade")
    inspector = sa.inspect(engine)
    assert not inspector.has_table("finding_signals")
    assert not inspector.has_table("posted_finding_comments")
    for table in TABLES:
        assert inspector.has_table(table)
        assert not set(SETTINGS) & set(_columns(engine, table))
    _run(engine, "downgrade")  # nothing left to drop is not an error
