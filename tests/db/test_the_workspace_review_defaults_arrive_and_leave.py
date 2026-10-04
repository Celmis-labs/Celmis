"""Migration e4c8a1f7b2d9: workspace review defaults, and repo fields that inherit.

Executed against SQLite (like its siblings) for what is not visible as text:

  * no repository's effective review changes on upgrade — a stored value
    equal to the old built-in default becomes NULL ("inherit"), which with an
    empty defaults table resolves to that same value, and every value that
    differs from it stays the repository's own;
  * the four columns stop holding a default nobody chose;
  * the revision reverses, writing the built-in defaults back.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles

from src.review.review_defaults import install_defaults, resolve
from src.review.settings import ReviewSettings

REVISION = "e4c8a1f7b2d9"
MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic" / "versions" / f"{REVISION}_workspace_review_defaults.py"
)
FIELDS = ("disabled_agents", "target_branches", "summary_enabled", "started_comment_enabled")


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw) -> str:  # pragma: no cover
    return "JSON"


def _migration():
    spec = importlib.util.spec_from_file_location(f"migration_{REVISION}", MIGRATION)
    assert spec and spec.loader, f"cannot load {MIGRATION}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _legacy_table() -> sa.Table:
    """`repo_review_policies` as d7a3e9c51b64 left it (the columns this
    migration touches, plus enough of the rest to be a policy)."""
    return sa.Table(
        "repo_review_policies", sa.MetaData(),
        sa.Column("repo_slug", sa.Text, primary_key=True),
        sa.Column("workspace_id", sa.Text, nullable=False, server_default="default"),
        sa.Column("prompt_template", sa.Text, nullable=False, server_default=""),
        sa.Column("target_branches", JSONB, nullable=False, server_default="[]"),
        sa.Column("disabled_agents", JSONB, nullable=False, server_default="[]"),
        sa.Column("summary_enabled", sa.Boolean, nullable=True, server_default=sa.true()),
        sa.Column("started_comment_enabled", sa.Boolean, nullable=True,
                  server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True)),
    )


DEFAULTED = {"repo_slug": "acme/defaults", "target_branches": [], "disabled_agents": [],
             "summary_enabled": True, "started_comment_enabled": True}
DECIDED = {"repo_slug": "acme/decided", "target_branches": ["main"],
           "disabled_agents": ["security"], "summary_enabled": False,
           "started_comment_enabled": False}


@pytest.fixture
def engine(tmp_path):
    e = sa.create_engine(f"sqlite:///{tmp_path}/celmis.db")
    legacy = _legacy_table()
    legacy.metadata.create_all(e)
    now = dt.datetime.now(dt.UTC)
    with e.begin() as conn:
        for row in (DEFAULTED, DECIDED):
            conn.execute(legacy.insert().values(
                workspace_id="ws-1", prompt_template="rules", created_at=now, **row))
    try:
        yield e
    finally:
        e.dispose()


def _run(engine, direction: str) -> None:
    module = _migration()
    with engine.begin() as conn:
        module.op = Operations(MigrationContext.configure(conn))
        getattr(module, direction)()


def _rows(engine) -> dict[str, dict]:
    table = sa.Table(
        "repo_review_policies", sa.MetaData(),
        sa.Column("repo_slug", sa.Text, primary_key=True),
        sa.Column("prompt_template", sa.Text),
        *(sa.Column(f, sa.JSON) for f in ("target_branches", "disabled_agents")),
        *(sa.Column(f, sa.Boolean) for f in ("summary_enabled", "started_comment_enabled")),
    )
    with engine.connect() as conn:
        return {r.repo_slug: dict(r._mapping) for r in conn.execute(sa.select(table))}


def _effective(row: dict) -> dict:
    values, _ = resolve(row, None, install_defaults(ReviewSettings()))
    return {f: values[f] for f in FIELDS}


def test_it_chains_off_the_previous_head():
    module = _migration()
    assert module.revision == REVISION
    assert module.down_revision == "d7a3e9c51b64"


def test_no_repository_behaves_differently_after_the_upgrade(engine):
    before = {slug: _effective(row) for slug, row in _rows(engine).items()}
    _run(engine, "upgrade")
    rows = _rows(engine)
    after = {slug: _effective(row) for slug, row in rows.items()}
    assert after == before

    # The defaulted row now inherits; the decided one keeps every answer.
    assert all(rows["acme/defaults"][f] is None for f in FIELDS)
    assert {f: rows["acme/decided"][f] for f in FIELDS} == {
        f: DECIDED[f] for f in FIELDS}
    assert rows["acme/decided"]["prompt_template"] == "rules"


def test_the_defaults_table_arrives_and_new_rows_inherit(engine):
    _run(engine, "upgrade")
    inspector = sa.inspect(engine)
    assert inspector.has_table("workspace_review_defaults")
    columns = {c["name"]: c for c in inspector.get_columns("workspace_review_defaults")}
    assert {"workspace_id", "disabled_agents", "verifier_enabled", "comment_min_severity",
            "max_inline_comments", "summary_enabled", "summary_instructions",
            "started_comment_enabled", "ignore_globs", "target_branches",
            "suppressed_rules", "updated_by"} <= set(columns)
    policy = {c["name"]: c for c in inspector.get_columns("repo_review_policies")}
    for f in FIELDS:
        assert policy[f]["nullable"], f
        assert policy[f]["default"] is None, f
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO repo_review_policies (repo_slug, workspace_id, prompt_template) "
            "VALUES ('acme/new', 'ws-1', '')"))
    assert all(_rows(engine)["acme/new"][f] is None for f in FIELDS)


def test_it_is_idempotent_and_reverses(engine):
    _run(engine, "upgrade")
    _run(engine, "upgrade")
    _run(engine, "downgrade")
    assert not sa.inspect(engine).has_table("workspace_review_defaults")
    rows = _rows(engine)
    assert {f: rows["acme/defaults"][f] for f in FIELDS} == {
        f: DEFAULTED[f] for f in FIELDS}
    assert {f: rows["acme/decided"][f] for f in FIELDS} == {f: DECIDED[f] for f in FIELDS}
    policy = {c["name"]: c for c in sa.inspect(engine).get_columns("repo_review_policies")}
    assert not policy["disabled_agents"]["nullable"]
    assert not policy["target_branches"]["nullable"]
