"""Per-repo `branch` column on auto_review_config.

auto_review_config is a standalone SQLite table (NOT Postgres/Alembic), so the
column is added by the same idempotent in-app migration pattern as
workspace_id. These tests pin the two things that can break a deployed
instance: a pre-existing DB with no `branch` column must be migrated in place
(and its legacy rows must read back as None = "default branch"), and the
migration must be safe to run twice.
"""

from __future__ import annotations

import sqlite3

from src.api.auto_review import AutoReviewStore, RepoConfig

_LEGACY_SCHEMA = """
CREATE TABLE auto_review_config (
    user_id TEXT NOT NULL, repo_slug TEXT NOT NULL, provider TEXT NOT NULL,
    full_name TEXT NOT NULL, url TEXT NOT NULL,
    workspace_id TEXT NOT NULL DEFAULT 'default',
    enabled INTEGER NOT NULL DEFAULT 0,
    mode TEXT NOT NULL DEFAULT 'polling', last_poll_etag TEXT,
    last_seen_pr_id INTEGER, last_polled_at TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, repo_slug))
"""


def _columns(path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(auto_review_config)")}
    finally:
        conn.close()


def test_fresh_schema_has_branch(tmp_path):
    p = tmp_path / "fresh.db"
    AutoReviewStore(p)
    assert "branch" in _columns(p)


def test_legacy_db_without_branch_is_migrated(tmp_path):
    """An old DB (no `branch` column) must not blow up the store, and its rows
    must come back with branch=None rather than a KeyError."""
    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.execute(_LEGACY_SCHEMA)
    c.execute(
        "INSERT INTO auto_review_config "
        "(user_id,repo_slug,provider,full_name,url,created_at,updated_at) "
        "VALUES ('u1','gh_owner-repo','github','owner/repo','https://x','t','t')"
    )
    c.commit()
    c.close()

    store = AutoReviewStore(p)  # triggers the idempotent ALTER
    assert "branch" in _columns(p)

    cfg = store.get("u1", "gh_owner-repo")
    assert cfg is not None
    assert cfg.branch is None  # legacy row → "whatever the default branch is"

    # And every other read path stays intact.
    assert store.list_for_workspace("default")[0].branch is None
    assert store.list_all()[0].branch is None


def test_migration_is_idempotent_and_branch_round_trips(tmp_path):
    p = tmp_path / "old2.db"
    c = sqlite3.connect(p)
    c.execute(_LEGACY_SCHEMA)
    c.commit()
    c.close()

    AutoReviewStore(p)
    store = AutoReviewStore(p)  # second init must not raise "duplicate column"

    store.upsert(RepoConfig(
        user_id="u1", repo_slug="gh_o-r", provider="github",
        full_name="o/r", url="https://x", workspace_id="wsA", branch="dev",
    ))
    assert store.get("u1", "gh_o-r").branch == "dev"
    assert store.get_in_workspace("wsA", "gh_o-r").branch == "dev"

    # Reset to the default branch — stored as NULL, read back as None.
    cfg = store.get("u1", "gh_o-r")
    cfg.branch = None
    store.upsert(cfg)
    assert store.get("u1", "gh_o-r").branch is None


def test_blank_branch_normalises_to_none(tmp_path):
    """An empty string in the column must not become a literal '' branch —
    clone_or_update treats "" and None very differently."""
    store = AutoReviewStore(tmp_path / "blank.db")
    store.upsert(RepoConfig(
        user_id="u1", repo_slug="gh_o-r", provider="github",
        full_name="o/r", url="https://x", branch="",
    ))
    assert store.get("u1", "gh_o-r").branch is None
