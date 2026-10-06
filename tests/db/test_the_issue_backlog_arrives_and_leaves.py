"""Migration b0e5a7c9d145: the issues backlog columns, the recheck state, the policy columns.

Executed against SQLite (like its siblings) for what is not visible as text:

  * every new column arrives NULLABLE with no server default (a row that
    predates them reads NULL, which the resolver turns into the built-ins);
  * the backfill gives issues of PRs already recorded as merged their
    `merged_at` / `base_ref` and the outcome their status implies, makes the
    issues a closed PR resolved `abandoned`, and leaves an issue with no PR row
    (or a PR with no base branch) without a backlog position;
  * it runs twice without harm and reverses, keeping what it did not add.
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

from src.db.models import (
    RepoReviewPolicy,
    ReviewIssue,
    ReviewPullRequest,
    WorkspaceReviewDefaults,
)

REVISION = "b0e5a7c9d145"
MIGRATION = (Path(__file__).resolve().parents[2] / "alembic" / "versions"
             / f"{REVISION}_issue_backlog_autoresolve.py")

ISSUE_COLUMNS = (
    "base_ref", "merged_at", "close_outcome", "dup_of", "snippet", "last_checked_at",
    "last_checked_sha", "last_checked_blob", "last_verified_blob", "fixed_by_pr_number",
    "fixed_by_pr_url", "resolution_note",
)
POLICY_COLUMNS = (
    "issues_auto_resolve", "issues_resolve_llm_verify", "issues_resolve_max_llm",
    "issues_announce_resolved",
)


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw) -> str:  # pragma: no cover
    return "JSON"


def _migration():
    spec = importlib.util.spec_from_file_location(f"migration_{REVISION}", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _legacy(model, drop) -> list[sa.Column]:
    return [
        sa.Column(c.name, c.type, primary_key=c.primary_key, nullable=c.nullable,
                  server_default=c.server_default.arg if c.server_default is not None else None)
        for c in model.__table__.columns if c.name not in drop
    ]


NOW = dt.datetime(2026, 9, 20, tzinfo=dt.UTC)


@pytest.fixture
def engine(tmp_path):
    e = sa.create_engine(f"sqlite:///{tmp_path}/celmis.db")
    md = sa.MetaData()
    issues = sa.Table("review_issues", md, *_legacy(ReviewIssue, ISSUE_COLUMNS))
    prs = sa.Table("review_pull_requests", md, *_legacy(ReviewPullRequest, ()))
    sa.Table("repo_review_policies", md, *_legacy(RepoReviewPolicy, POLICY_COLUMNS))
    sa.Table("workspace_review_defaults", md, *_legacy(WorkspaceReviewDefaults, POLICY_COLUMNS))
    md.create_all(e)

    def pr(number, state, base="main"):
        return dict(id=f"p{number}", workspace_id="ws", provider="github", repo="o/r", number=number,
                    state=state, base_ref=base, closed_at=NOW if state != "open" else None,
                    title="", reviews_count=1, opened_at=NOW, updated_at=NOW)

    def issue(n, number, status, source=None):
        return dict(id=f"i{n}", workspace_id="ws", repo_slug="o-r", fingerprint=f"fp{n}",
                    file_path="a.py", status=status, resolution_source=source,
                    pr_provider="github", pr_repo="o/r", pr_number=number,
                    first_seen_at=NOW, last_seen_at=NOW, title="t", body="b")

    with e.begin() as conn:
        conn.execute(prs.insert(), [pr(1, "merged"), pr(2, "closed"), pr(3, "open"),
                                    pr(4, "merged", base=None)])
        conn.execute(issues.insert(), [
            issue(1, 1, "open"), issue(2, 1, "fixed", "auto_next_commit"),
            issue(3, 1, "dismissed", "manual"), issue(4, 2, "resolved", "pr_closed"),
            issue(5, 3, "open"), issue(6, 99, "open"), issue(7, 4, "open"),
        ])
    try:
        yield e
    finally:
        e.dispose()


def _run(engine, direction):
    module = _migration()
    with engine.begin() as conn:
        module.op = Operations(MigrationContext.configure(conn))
        getattr(module, direction)()


def _issue_rows(engine) -> dict[str, dict]:
    with engine.connect() as conn:
        return {r["id"]: dict(r) for r in conn.execute(sa.text("SELECT * FROM review_issues")).mappings()}


def test_it_chains_off_the_revision_before_it() -> None:
    module = _migration()
    assert module.revision == REVISION
    assert module.down_revision == "f8c3e5a7b923"


def test_every_new_column_arrives_nullable_without_a_default(engine) -> None:
    _run(engine, "upgrade")
    inspector = sa.inspect(engine)
    issue_cols = {c["name"]: c for c in inspector.get_columns("review_issues")}
    for name in ISSUE_COLUMNS:
        assert issue_cols[name]["nullable"] and issue_cols[name]["default"] is None, name
    for table in ("repo_review_policies", "workspace_review_defaults"):
        cols = {c["name"]: c for c in inspector.get_columns(table)}
        for name in POLICY_COLUMNS:
            assert cols[name]["nullable"] and cols[name]["default"] is None, f"{table}.{name}"
    assert inspector.has_table("review_issue_recheck_state")
    indexes = {i["name"] for i in inspector.get_indexes("review_issues")}
    assert {"ix_review_issues_backlog", "ix_review_issues_dup_of"} <= indexes


def test_the_backfill_gives_merged_prs_issues_their_position_and_fate(engine) -> None:
    _run(engine, "upgrade")
    rows = _issue_rows(engine)
    assert (rows["i1"]["merged_at"] is not None, rows["i1"]["base_ref"],
            rows["i1"]["close_outcome"]) == (True, "main", "unimplemented")
    assert rows["i2"]["close_outcome"] == "implemented"
    assert rows["i3"]["close_outcome"] == "dismissed"
    # A closed PR's issue is abandoned, and is not on a branch.
    assert (rows["i4"]["close_outcome"], rows["i4"]["merged_at"]) == ("abandoned", None)
    # An open PR's issue, and one with no PR row, have no fate yet.
    for key in ("i5", "i6"):
        assert (rows[key]["merged_at"], rows[key]["close_outcome"]) == (None, None)
    # A merged PR with no known base branch is merged but not on a branch.
    assert rows["i7"]["merged_at"] is not None and rows["i7"]["base_ref"] is None


def test_it_is_idempotent_and_reverses_keeping_what_it_did_not_add(engine) -> None:
    _run(engine, "upgrade")
    with engine.begin() as conn:
        conn.execute(sa.text("UPDATE review_issues SET close_outcome='implemented' WHERE id='i1'"))
    _run(engine, "upgrade")      # a second run leaves what the application wrote
    assert _issue_rows(engine)["i1"]["close_outcome"] == "implemented"
    _run(engine, "downgrade")
    inspector = sa.inspect(engine)
    cols = {c["name"] for c in inspector.get_columns("review_issues")}
    assert not cols & set(ISSUE_COLUMNS)
    assert not inspector.has_table("review_issue_recheck_state")
    for table in ("repo_review_policies", "workspace_review_defaults"):
        assert not {c["name"] for c in inspector.get_columns(table)} & set(POLICY_COLUMNS)
    assert _issue_rows(engine)["i1"]["status"] == "open"
    _run(engine, "downgrade")    # nothing left to drop
    _run(engine, "upgrade")
