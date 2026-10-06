"""Migration e7b2d4f6a812: the cadence and title-keyword settings and the per-PR state.

Four nullable policy columns on both tables, nine columns on
`review_pull_requests` (`review_paused` NOT NULL, false), a backfilled
baseline for PRs whose last review was complete, idempotent upgrade, and a
downgrade that drops exactly what was added and keeps the rest.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from src.db.models import RepoReviewPolicy, ReviewPullRequest, WorkspaceReviewDefaults

REVISION = "e7b2d4f6a812"
MIGRATION = (Path(__file__).resolve().parents[2] / "alembic" / "versions"
             / f"{REVISION}_review_cadence_and_title_gate.py")

POLICY_COLUMNS = ("review_cadence", "auto_pause_pushes", "auto_pause_window_minutes",
                  "ignored_title_keywords")
PR_COLUMNS = ("last_reviewed_sha", "last_reviewed_at", "last_seen_sha", "recent_pushes",
              "review_paused", "paused_reason", "paused_at", "paused_by",
              "pause_notice_at")
TABLES = {"repo_review_policies": RepoReviewPolicy,
          "workspace_review_defaults": WorkspaceReviewDefaults,
          "review_pull_requests": ReviewPullRequest}


def _migration():
    spec = importlib.util.spec_from_file_location("m_e7b2d4f6a812", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _legacy(table: str) -> list[sa.Column]:
    """The table as d6a1c3e5f701 left it: the model's columns minus the new."""
    new = PR_COLUMNS if table == "review_pull_requests" else POLICY_COLUMNS
    return [
        sa.Column(c.name, sa.JSON() if "JSON" in type(c.type).__name__.upper() else c.type,
                  primary_key=c.primary_key, nullable=c.nullable or c.name in new,
                  server_default=sa.text("''") if c.name == "prompt_template" else None)
        for c in TABLES[table].__table__.columns if c.name not in new
    ]


@pytest.fixture
def engine(tmp_path):
    e = sa.create_engine(f"sqlite:///{tmp_path}/celmis.db")
    md = sa.MetaData()
    for table in TABLES:
        sa.Table(table, md, *_legacy(table))
    md.create_all(e)
    with e.begin() as conn:
        for pr_id, status, head in (("done", "complete", "aaa"), ("failed", "failed", "bbb"),
                                    ("nohead", "complete", None)):
            conn.execute(sa.text(
                "INSERT INTO review_pull_requests (id, workspace_id, provider, repo, number, "
                "state, reviews_count, title, opened_at, updated_at, head_sha, "
                "last_review_status) VALUES (:id, 'ws-1', 'github', 'o/r', :n, 'open', 1, "
                "'t', :now, :now, :head, :status)"),
                {"id": pr_id, "n": hash(pr_id) % 1000, "now": dt.datetime.now(dt.UTC),
                 "head": head, "status": status})
    yield e
    e.dispose()


def _run(engine, direction: str) -> None:
    module = _migration()
    with engine.begin() as conn:
        module.op = Operations(MigrationContext.configure(conn))
        getattr(module, direction)()


def _columns(engine, table: str) -> dict[str, dict]:
    return {c["name"]: c for c in sa.inspect(engine).get_columns(table)}


def test_it_chains_off_the_previous_revision():
    module = _migration()
    assert (module.revision, module.down_revision) == (REVISION, "d6a1c3e5f701")


def test_the_policy_columns_arrive_nullable_on_both_tables(engine):
    _run(engine, "upgrade")
    for table in ("repo_review_policies", "workspace_review_defaults"):
        columns = _columns(engine, table)
        for name in POLICY_COLUMNS:
            assert name in columns and columns[name]["nullable"], f"{table}.{name}"
            assert columns[name]["default"] is None, f"{table}.{name}: NULL means inherit"


def test_the_pr_state_columns_arrive_and_a_pr_starts_unpaused(engine):
    _run(engine, "upgrade")
    columns = _columns(engine, "review_pull_requests")
    assert all(name in columns for name in PR_COLUMNS)
    assert columns["review_paused"]["nullable"] is False
    with engine.connect() as conn:
        paused = conn.execute(sa.text(
            "SELECT DISTINCT review_paused FROM review_pull_requests")).scalars().all()
    assert [bool(p) for p in paused] == [False]


def test_only_a_complete_review_leaves_a_baseline(engine):
    _run(engine, "upgrade")
    with engine.connect() as conn:
        got = dict(conn.execute(sa.text(
            "SELECT id, last_reviewed_sha FROM review_pull_requests")).all())
    assert got == {"done": "aaa", "failed": None, "nohead": None}


def test_upgrading_twice_changes_nothing(engine):
    _run(engine, "upgrade")
    with engine.begin() as conn:
        conn.execute(sa.text(
            "UPDATE review_pull_requests SET last_reviewed_sha = 'kept' WHERE id = 'done'"))
    _run(engine, "upgrade")
    with engine.connect() as conn:
        assert conn.execute(sa.text(
            "SELECT last_reviewed_sha FROM review_pull_requests WHERE id = 'done'"
        )).scalar_one() == "kept"


def test_downgrade_drops_what_was_added_and_keeps_the_rows(engine):
    _run(engine, "upgrade")
    _run(engine, "downgrade")
    for table, names in (("repo_review_policies", POLICY_COLUMNS),
                         ("workspace_review_defaults", POLICY_COLUMNS),
                         ("review_pull_requests", PR_COLUMNS)):
        columns = _columns(engine, table)
        assert not [n for n in names if n in columns], table
    with engine.connect() as conn:
        assert conn.execute(sa.text(
            "SELECT count(*) FROM review_pull_requests")).scalar_one() == 3
    _run(engine, "downgrade")  # again: nothing left to drop
    _run(engine, "upgrade")
    assert "review_cadence" in _columns(engine, "repo_review_policies")
