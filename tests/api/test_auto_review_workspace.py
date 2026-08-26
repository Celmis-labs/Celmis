"""Multi-tenant auto_review_config: workspace_id column + deterministic
repo->workspace routing (webhook fail-closed).

auto_review_config is a standalone SQLite table (NOT Postgres/Alembic), so the
workspace_id column is added via an idempotent in-app migration. These tests
pin: fresh schema has the column, a pre-existing DB is migrated in place and its
legacy rows default to 'default', and the repo->workspace lookup fails closed
when a repo is bound to more than one workspace.
"""

from __future__ import annotations

import sqlite3

from src.api.auto_review import AutoReviewStore, RepoConfig


def test_fresh_schema_has_workspace_id(tmp_path):
    p = tmp_path / "fresh.db"
    AutoReviewStore(p)
    cols = {r[1] for r in sqlite3.connect(p).execute("PRAGMA table_info(auto_review_config)")}
    assert "workspace_id" in cols


def test_legacy_db_migrated_in_place(tmp_path):
    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.execute(
        """CREATE TABLE auto_review_config (
            user_id TEXT NOT NULL, repo_slug TEXT NOT NULL, provider TEXT NOT NULL,
            full_name TEXT NOT NULL, url TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
            mode TEXT NOT NULL DEFAULT 'polling', last_poll_etag TEXT, last_seen_pr_id INTEGER,
            last_polled_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY (user_id, repo_slug))"""
    )
    c.execute(
        "INSERT INTO auto_review_config (user_id,repo_slug,provider,full_name,url,created_at,updated_at)"
        " VALUES ('u1','gh_owner-repo','github','owner/repo','https://x','t','t')"
    )
    c.commit()
    c.close()

    store = AutoReviewStore(p)  # triggers the idempotent ALTER
    cols = {r[1] for r in sqlite3.connect(p).execute("PRAGMA table_info(auto_review_config)")}
    assert "workspace_id" in cols
    cfg = store.get("u1", "gh_owner-repo")
    assert cfg is not None
    assert cfg.workspace_id == "default"  # legacy rows backfill to the default tenant


def test_workspace_for_repo_deterministic(tmp_path):
    store = AutoReviewStore(tmp_path / "wf.db")
    store.upsert(RepoConfig(
        user_id="u1", repo_slug="gh_o-r", provider="github",
        full_name="o/r", url="x", workspace_id="wsA",
    ))
    assert store.workspace_for_repo("github", "o/r") == "wsA"
    assert store.existing_workspace_binding("github", "o/r") == "wsA"
    assert store.existing_workspace_binding("github", "o/unknown") is None


def test_workspace_for_repo_fails_closed_when_ambiguous(tmp_path):
    """A repo registered under two workspaces must resolve to None so the
    unauthenticated webhook fails closed instead of running under the wrong
    tenant's keys."""
    store = AutoReviewStore(tmp_path / "amb.db")
    store.upsert(RepoConfig(user_id="u1", repo_slug="gh_o-r", provider="github",
                            full_name="o/r", url="x", workspace_id="wsA"))
    store.upsert(RepoConfig(user_id="u2", repo_slug="gh_o-r", provider="github",
                            full_name="o/r", url="x", workspace_id="wsB"))
    assert store.workspace_for_repo("github", "o/r") is None
