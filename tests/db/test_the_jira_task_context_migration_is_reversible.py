"""Migration d2a7c9e1f367: the Jira task context, stored.

Executed against SQLite for what is not visible as text:

  * the five settings and the two requirements-check columns arrive NULLABLE with
    no server default on BOTH policy tables, a row that predates them reads NULL
    everywhere, and NULL resolves to the built-ins (no behaviour change);
  * `review_pull_requests` gets `task_refs` and `requirements_check`;
  * `task_context_cache` is created with its uniqueness, so two workers cannot
    store two reads of one task;
  * running it twice is harmless and it reverses, leaving every other column and
    row as it was.
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

from src.review.review_defaults import BUILTIN_DEFAULTS, install_defaults, resolve
from src.review.settings import ReviewSettings

REVISION = "d2a7c9e1f367"
MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic" / "versions" / f"{REVISION}_jira_task_context.py"
)
SETTINGS = ("task_context_enabled", "task_project_keys", "task_acceptance_field",
            "task_include_comments", "business_logic_auto")
EXTRA = ("requirements_check_mode", "task_urls_enabled")
PR_COLUMNS = ("task_refs", "requirements_check")
POLICY_TABLES = ("repo_review_policies", "workspace_review_defaults")


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw) -> str:  # pragma: no cover
    return "JSON"


def _migration():
    spec = importlib.util.spec_from_file_location(f"migration_{REVISION}", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _legacy(model, drop: tuple[str, ...]) -> list[sa.Column]:
    return [
        sa.Column(c.name, c.type, primary_key=c.primary_key, nullable=c.nullable,
                  server_default=c.server_default.arg if c.server_default is not None else None)
        for c in model.__table__.columns if c.name not in drop
    ]


@pytest.fixture
def engine(tmp_path):
    from src.db.models import RepoReviewPolicy, ReviewPullRequest, WorkspaceReviewDefaults

    e = sa.create_engine(f"sqlite:///{tmp_path}/celmis.db")
    md = sa.MetaData()
    policy = sa.Table("repo_review_policies", md,
                      *_legacy(RepoReviewPolicy, (*SETTINGS, *EXTRA)))
    defaults = sa.Table("workspace_review_defaults", md,
                        *_legacy(WorkspaceReviewDefaults, (*SETTINGS, *EXTRA)))
    prs = sa.Table("review_pull_requests", md, *_legacy(ReviewPullRequest, PR_COLUMNS))
    md.create_all(e)
    now = dt.datetime.now(dt.UTC)
    with e.begin() as conn:
        conn.execute(policy.insert().values(
            repo_slug="acme/api", workspace_id="ws-1", enabled=True, prompt_template="rules",
            folder_rules=[], agent_prompt_overrides={}, mcp_sources=[],
            disabled_agents=["cve"], summary_enabled=False, created_at=now, updated_at=now))
        conn.execute(defaults.insert().values(
            workspace_id="ws-1", disabled_agents=["structural"],
            summary_instructions="be brief", created_at=now, updated_at=now))
        conn.execute(prs.insert().values(
            id="pr-1", workspace_id="ws-1", provider="github", repo="acme/api", number=7))
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


def _row(engine, table: str) -> dict:
    with engine.connect() as conn:
        return dict(conn.execute(sa.text(f"SELECT * FROM {table}")).mappings().one())


def test_it_chains_off_the_issues_backlog_migration():
    module = _migration()
    assert module.revision == REVISION
    assert module.down_revision == "b0e5a7c9d145"


def test_every_setting_arrives_nullable_with_no_default_on_both_tables(engine):
    _run(engine, "upgrade")
    for table in POLICY_TABLES:
        columns = _columns(engine, table)
        for name in (*SETTINGS, *EXTRA):
            assert name in columns, f"{table}.{name}"
            assert columns[name]["nullable"], f"{table}.{name}"
            assert columns[name]["default"] is None, f"{table}.{name}"


def test_existing_rows_behave_exactly_as_before(engine):
    _run(engine, "upgrade")
    policy = _row(engine, "repo_review_policies")
    defaults = _row(engine, "workspace_review_defaults")
    assert all(policy[n] is None for n in (*SETTINGS, *EXTRA))
    assert all(defaults[n] is None for n in (*SETTINGS, *EXTRA))
    assert policy["prompt_template"] == "rules" and defaults["summary_instructions"] == "be brief"
    values, sources = resolve(policy, {n: defaults[n] for n in SETTINGS},
                              install_defaults(ReviewSettings()))
    for name in SETTINGS:
        assert values[name] == BUILTIN_DEFAULTS[name], name
        assert sources[name] == "install", name


def test_the_pull_request_row_gets_its_two_json_columns_and_keeps_its_data(engine):
    _run(engine, "upgrade")
    columns = _columns(engine, "review_pull_requests")
    for name in PR_COLUMNS:
        assert name in columns and columns[name]["nullable"]
    row = _row(engine, "review_pull_requests")
    assert row["id"] == "pr-1" and row["task_refs"] is None and row["requirements_check"] is None


def test_the_cache_table_arrives_and_keeps_one_read_per_task(engine):
    _run(engine, "upgrade")
    columns = _columns(engine, "task_context_cache")
    assert {"workspace_id", "site_host", "issue_key", "payload", "status", "fetched_at"} <= set(columns)
    insp = sa.inspect(engine)
    unique = [tuple(u["column_names"]) for u in insp.get_unique_constraints("task_context_cache")]
    assert ("workspace_id", "site_host", "issue_key") in unique
    assert "ix_task_context_cache_ws_fetched" in {i["name"] for i in insp.get_indexes("task_context_cache")}
    with engine.begin() as conn:
        insert = sa.text(
            "INSERT INTO task_context_cache (id, workspace_id, site_host, issue_key, payload, status) "
            "VALUES (:id, 'ws', 'h', 'PROJ-1', '{}', 'ok')")
        conn.execute(insert, {"id": "a"})
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
        conn.execute(insert, {"id": "b"})


def test_it_is_idempotent_and_reverses_leaving_everything_else(engine):
    _run(engine, "upgrade")
    _run(engine, "upgrade")
    _run(engine, "downgrade")
    for table in POLICY_TABLES:
        columns = _columns(engine, table)
        for name in (*SETTINGS, *EXTRA):
            assert name not in columns, f"{table}.{name}"
    assert not (set(PR_COLUMNS) & set(_columns(engine, "review_pull_requests")))
    assert not sa.inspect(engine).has_table("task_context_cache")
    assert _row(engine, "repo_review_policies")["prompt_template"] == "rules"
    assert _row(engine, "workspace_review_defaults")["summary_instructions"] == "be brief"
    assert _row(engine, "review_pull_requests")["id"] == "pr-1"
    _run(engine, "downgrade")             # again: nothing left to drop
    _run(engine, "upgrade")               # and back up
    assert "task_refs" in _columns(engine, "review_pull_requests")
