"""The migration that gives the repo policy its review-output settings.

Executed against sqlite (like its siblings for `suppressed_rules` and
`agent_llm_overrides`) for the two properties that are not visible as text:
a row that predates the columns keeps posting its summary and its "review
started" comment (server default TRUE, never NULL-as-off), and the revision
reverses.
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

REVISION = "d7a3e9c51b64"
COLUMNS = {
    "summary_enabled", "summary_instructions", "started_comment_enabled",
    "review_language", "max_inline_comments",
}
MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic" / "versions" / f"{REVISION}_policy_review_output.py"
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
    import datetime as dt

    e = sa.create_engine(f"sqlite:///{tmp_path}/celmis.db")
    legacy = sa.Table(
        RepoReviewPolicy.__tablename__, sa.MetaData(),
        *[
            sa.Column(c.name, c.type, primary_key=c.primary_key, nullable=c.nullable)
            for c in RepoReviewPolicy.__table__.columns if c.name not in COLUMNS
        ],
    )
    legacy.create(e)
    now = dt.datetime.now(dt.UTC)
    with e.begin() as conn:
        conn.execute(legacy.insert().values(
            repo_slug="acme/api", workspace_id="default", enabled=True,
            prompt_template="rules", target_branches=[], folder_rules=[],
            agent_prompt_overrides={}, mcp_sources=[], disabled_agents=[],
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
    assert module.down_revision == "f6b1d3a8c240"


def test_an_existing_policy_keeps_its_summary_and_started_comment(engine):
    _run(engine, "upgrade")
    assert _columns(engine) >= COLUMNS
    with Session(engine) as session:
        row = session.get(RepoReviewPolicy, "acme/api")
        assert row.summary_enabled is True
        assert row.started_comment_enabled is True
        assert row.summary_instructions is None
        assert row.review_language is None
        assert row.max_inline_comments is None
        assert row.prompt_template == "rules", "additive: the row is otherwise untouched"


def test_it_is_idempotent_and_reverses(engine):
    _run(engine, "upgrade")
    _run(engine, "upgrade")
    _run(engine, "downgrade")
    assert not (COLUMNS & _columns(engine))
    with engine.connect() as conn:
        assert conn.execute(sa.text(
            "SELECT count(*) FROM repo_review_policies"
        )).scalar_one() == 1
