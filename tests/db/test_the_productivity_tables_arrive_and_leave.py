"""Migration e3b8d0f2a478: the productivity tables arrive and leave.

Executed against SQLite like its siblings: the revision chains where the lane
was cut, every model's columns are in the table the migration makes, the ORM
can write through it, and it reverses.
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

REVISION = "e3b8d0f2a478"
MIGRATION = (Path(__file__).resolve().parents[2] / "alembic" / "versions"
             / f"{REVISION}_productivity_metrics.py")


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw) -> str:  # pragma: no cover
    return "JSON"


def _migration():
    spec = importlib.util.spec_from_file_location(f"migration_{REVISION}", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def engine(tmp_path):
    e = sa.create_engine(f"sqlite:///{tmp_path}/celmis.db")
    try:
        yield e
    finally:
        e.dispose()


def _run(engine, direction: str) -> None:
    module = _migration()
    with engine.begin() as conn:
        module.op = Operations(MigrationContext.configure(conn))
        getattr(module, direction)()


def _models():
    from src.db.models import (
        ProductivityDeployment,
        ProductivityDeploymentPr,
        ProductivityPrEvent,
        ProductivityPullRequest,
        ProductivityRepoSettings,
        ProductivitySyncState,
    )

    return (ProductivityRepoSettings, ProductivityPullRequest, ProductivityPrEvent,
            ProductivityDeployment, ProductivityDeploymentPr, ProductivitySyncState)


def test_it_chains_off_the_jira_task_context_migration() -> None:
    module = _migration()
    assert module.revision == REVISION
    assert module.down_revision == "d2a7c9e1f367"


def test_every_table_arrives_with_exactly_the_columns_the_model_maps(engine) -> None:
    _run(engine, "upgrade")
    inspector = sa.inspect(engine)
    for model in _models():
        table = model.__tablename__
        assert inspector.has_table(table), table
        assert {c["name"] for c in inspector.get_columns(table)} == {c.name for c in model.__table__.columns}, table


def test_the_indexes_and_unique_keys_the_models_declare_arrive_too(engine) -> None:
    _run(engine, "upgrade")
    inspector = sa.inspect(engine)
    for model in _models():
        wanted = {i.name for i in model.__table__.indexes}
        have = {i["name"] for i in inspector.get_indexes(model.__tablename__)}
        assert wanted <= have, (model.__tablename__, wanted - have)
        uniques = {u["name"] for u in inspector.get_unique_constraints(model.__tablename__)}
        declared = {c.name for c in model.__table__.constraints
                    if isinstance(c, sa.UniqueConstraint) and c.name}
        assert declared <= uniques, (model.__tablename__, declared - uniques)


def test_the_orm_writes_and_reads_through_the_migrated_tables_with_their_defaults(engine) -> None:
    from src.db.models import ProductivityPullRequest, ProductivitySyncState

    _run(engine, "upgrade")
    with Session(engine) as s:
        s.add(ProductivityPullRequest(workspace_id="ws", provider="github", repo="o/r", number=1))
        s.add(ProductivitySyncState(workspace_id="ws", provider="github", repo="o/r"))
        s.commit()
        row = s.query(ProductivityPullRequest).one()
        assert (row.kind, row.state, row.detail_state, row.human_comments, row.is_draft) == (
            "feature", "open", "none", 0, False)
        state = s.query(ProductivitySyncState).one()
        assert (state.prs_total, state.backfill_done) == (0, False)


def test_a_pr_number_is_unique_within_its_repository(engine) -> None:
    from src.db.models import ProductivityPullRequest

    _run(engine, "upgrade")
    with Session(engine) as s:
        s.add(ProductivityPullRequest(workspace_id="ws", provider="github", repo="o/r", number=1))
        s.commit()
        s.add(ProductivityPullRequest(workspace_id="ws", provider="github", repo="o/r", number=1))
        with pytest.raises(sa.exc.IntegrityError):
            s.commit()


def test_it_reverses_and_leaves_nothing_behind(engine) -> None:
    _run(engine, "upgrade")
    _run(engine, "downgrade")
    inspector = sa.inspect(engine)
    assert not [m.__tablename__ for m in _models() if inspector.has_table(m.__tablename__)]


def test_no_other_table_is_touched(engine) -> None:
    other = sa.Table("review_pull_requests", sa.MetaData(), sa.Column("id", sa.Text, primary_key=True))
    other.metadata.create_all(engine)
    _run(engine, "upgrade")
    _run(engine, "downgrade")
    assert sa.inspect(engine).has_table("review_pull_requests")
