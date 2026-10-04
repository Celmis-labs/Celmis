"""Migration a7b8c9d0e1f2: the review rules tables arrive and leave.

Executed against SQLite like its siblings:

  * it chains off the parallel revision f1a2b3c4d5e6;
  * both tables arrive with the columns the model maps, and the ORM can
    write and read a rule through them (server defaults included);
  * a policy's `folder_rules` are left exactly as they were — they are not
    copied, so the policy page stays their one home;
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

REVISION = "a7b8c9d0e1f2"
MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic" / "versions" / f"{REVISION}_review_rules.py"
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


@pytest.fixture
def engine(tmp_path):
    e = sa.create_engine(f"sqlite:///{tmp_path}/celmis.db")
    policies = sa.Table(
        "repo_review_policies", sa.MetaData(),
        sa.Column("repo_slug", sa.Text, primary_key=True),
        sa.Column("workspace_id", sa.Text),
        sa.Column("folder_rules", sa.JSON),
    )
    policies.metadata.create_all(e)
    with e.begin() as conn:
        conn.execute(policies.insert().values(
            repo_slug="acme/app", workspace_id="ws-1",
            folder_rules=[{"pattern": "src/**", "prompt": "keep it", "title": "Keep"}]))
    try:
        yield e
    finally:
        e.dispose()


def _run(engine, direction: str) -> None:
    module = _migration()
    with engine.begin() as conn:
        module.op = Operations(MigrationContext.configure(conn))
        getattr(module, direction)()


def test_it_chains_off_the_parallel_revision():
    module = _migration()
    assert module.revision == REVISION
    assert module.down_revision == "f1a2b3c4d5e6"


def test_the_tables_arrive_with_what_the_model_maps(engine):
    from src.db.models import ReviewRule, ReviewRuleJob

    _run(engine, "upgrade")
    inspector = sa.inspect(engine)
    for model in (ReviewRule, ReviewRuleJob):
        table = model.__tablename__
        assert inspector.has_table(table)
        columns = {c["name"] for c in inspector.get_columns(table)}
        assert {c.name for c in model.__table__.columns} == columns, table
    indexes = {i["name"] for i in inspector.get_indexes("review_rules")}
    assert "ix_review_rules_scope" in indexes


def test_a_rule_round_trips_through_the_migrated_table(engine):
    from src.db.models import ReviewRule

    _run(engine, "upgrade")
    with Session(engine) as s:
        s.add(ReviewRule(workspace_id="ws-1", repo_slug=None, title="T",
                         instructions="do", agents=[]))
        s.commit()
    with engine.connect() as conn:
        row = conn.execute(sa.text(
            "SELECT id, severity, status, origin, repo_slug FROM review_rules")).one()
    assert row.id == 1
    assert (row.severity, row.status, row.origin, row.repo_slug) == (
        "warning", "pending", "manual", None)


def test_folder_rules_are_not_touched(engine):
    _run(engine, "upgrade")
    with engine.connect() as conn:
        rules = conn.execute(sa.text("SELECT folder_rules FROM repo_review_policies")).scalar()
        count = conn.execute(sa.text("SELECT COUNT(*) FROM review_rules")).scalar()
    assert "keep it" in str(rules)
    assert count == 0


def test_it_is_idempotent_and_reverses(engine):
    _run(engine, "upgrade")
    _run(engine, "upgrade")
    _run(engine, "downgrade")
    inspector = sa.inspect(engine)
    assert not inspector.has_table("review_rules")
    assert not inspector.has_table("review_rule_jobs")
    assert inspector.has_table("repo_review_policies")
    _run(engine, "downgrade")  # nothing left to drop is not an error
