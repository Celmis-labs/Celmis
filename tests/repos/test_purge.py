"""Cascade repo purge across Qdrant / disk / Postgres / SQLite / groups.

`src/repos/purge.py` is the only place that knows the full list of stores a
repo leaves traces in, and both `analyzer repo purge` and
`DELETE /api/repos/{slug}?purge=true` read named fields off its report — so the
report shape is a contract, not an implementation detail.

Coverage:
    1. every store is cleared, and the report counts what it cleared
    2. unrelated rows / other repos survive
    3. one dead store does not block the others (errors are collected)
    4. group matching works on any identifier form (URL, owner/name, prefixed)
    5. skip_qdrant / skip_disk leave those stores untouched
    6. as_dict() carries the keys the web UI reads
    7. purging a repo that was never indexed is a no-op, not a crash
"""

from __future__ import annotations

import sqlite3

import pytest

from src.api.auto_review import AutoReviewStore, RepoConfig
from src.api.review_runs import ReviewRunStore
from src.config import Settings
from src.groups.manager import GroupManager
from src.repos.purge import purge_repo

FULL_NAME = "Acme-Dev/todo-app"
# ParsedRepo.slug is case-preserving: '{provider}_{owner}-{name}'.
SLUG = "github_Acme-Dev-todo-app"


# ─── Fakes ───────────────────────────────────────────────────────────


class FakeQdrant:
    """Counts 42 matching points, records the delete."""

    def __init__(self, *, exists: bool = True, count: int = 42) -> None:
        self._exists, self._count = exists, count
        self.deleted_with = None

    def collection_exists(self, collection_name):
        return self._exists

    def count(self, collection_name, count_filter, exact):
        import types
        self.filter_used = count_filter
        return types.SimpleNamespace(count=self._count)

    def delete(self, collection_name, points_selector):
        self.deleted_with = points_selector


class FakeSession:
    """Minimal AsyncSession stand-in — every DELETE reports `rowcount` rows."""

    def __init__(self, rowcount: int = 2) -> None:
        self._rowcount = rowcount
        self.executed: list = []
        self.committed = False

    async def execute(self, stmt):
        import types
        self.executed.append(stmt)
        return types.SimpleNamespace(rowcount=self._rowcount)

    async def commit(self):
        self.committed = True

    async def rollback(self):
        pass


# ─── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Real stores on tmp paths; Qdrant and Postgres faked."""
    settings = Settings(
        gemini_api_key="test-key",
        workspace_dir=tmp_path / "ws",
        vault_dir=tmp_path / "vault",
    )
    settings.ensure_directories()
    monkeypatch.setattr("src.config.get_settings", lambda: settings)
    # GroupManager does `from src.config import get_settings` at import time, so
    # it holds its own reference and the patch above never reaches it.
    monkeypatch.setattr("src.groups.manager.get_settings", lambda: settings)

    ar = AutoReviewStore(tmp_path / "auto_review.db")
    runs = ReviewRunStore(tmp_path / "review_runs.db")
    monkeypatch.setattr("src.api.auto_review.get_auto_review_store", lambda: ar)
    monkeypatch.setattr("src.api.review_runs.get_review_run_store", lambda: runs)

    qdrant = FakeQdrant()
    monkeypatch.setattr("src.retrieval.vector_store.get_vector_client", lambda: qdrant)

    import types
    return types.SimpleNamespace(
        settings=settings, ar=ar, runs=runs, qdrant=qdrant, tmp=tmp_path,
    )


def _seed_disk(settings, slug, *, size=1000):
    """Clone + derived-data + vault dirs, each with one file of `size` bytes."""
    paths = [settings.repo_path(slug), settings.repo_data_path(slug),
             settings.repo_vault_path(slug)]
    for p in paths:
        p.mkdir(parents=True, exist_ok=True)
        (p / "f.txt").write_bytes(b"x" * size)
    return paths


def _seed_auto_review(store, slug=SLUG, full_name=FULL_NAME, users=("u1", "u2")):
    for u in users:
        store.upsert(RepoConfig(
            user_id=u, repo_slug=slug, provider="github",
            full_name=full_name, url=f"https://github.com/{full_name}",
        ))


def _seed_runs(db_path, rows):
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.executemany(
        "INSERT INTO review_runs (id, user_id, pr_ref, status, started_at, pr_repo)"
        " VALUES (?,?,?,?,?,?)", rows,
    )
    conn.close()


def _count(db_path, table):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


# ─── Tests ───────────────────────────────────────────────────────────


async def test_purges_every_store_and_reports_counts(env):
    _seed_disk(env.settings, SLUG)
    _seed_auto_review(env.ar)
    _seed_runs(env.runs.db_path, [
        ("r1", "u1", f"github:{FULL_NAME}#1", "done", "t", FULL_NAME),
        ("r2", "u1", f"github:{FULL_NAME}#2", "done", "t", None),
    ])
    mgr = GroupManager(env.settings)
    g = mgr.create("g1")
    g.add_repo(f"https://github.com/{FULL_NAME}")
    mgr.save(g)

    session = FakeSession(rowcount=2)
    report = await purge_repo(SLUG, session=session)

    assert report.errors == []
    assert report.qdrant_points_deleted == 42
    assert env.qdrant.deleted_with is not None

    assert report.clone_dir_removed and report.data_dir_removed and report.vault_dir_removed
    assert report.disk_bytes_freed == 3000
    assert not env.settings.repo_path(SLUG).exists()
    assert not env.settings.repo_vault_path(SLUG).exists()

    assert report.project_repo_links_removed == 2
    assert report.orphan_rows_removed == 14   # 7 repo_slug-keyed tables x 2 rows
    assert session.committed

    assert report.auto_review_rows_removed == 2   # both users, not just the caller
    assert _count(env.ar.db_path, "auto_review_config") == 0
    # matched by pr_repo AND by pr_ref, so the row with a NULL pr_repo goes too
    assert report.review_run_rows_removed == 2
    assert _count(env.runs.db_path, "review_runs") == 0

    assert report.group_memberships_removed == 1
    assert report.groups_touched == ["g1"]
    assert mgr.load("g1").repos == []

    assert report.elapsed_seconds >= 0


async def test_leaves_other_repos_alone(env):
    _seed_disk(env.settings, SLUG)
    _seed_disk(env.settings, "github_someone-else")
    _seed_auto_review(env.ar)
    _seed_auto_review(env.ar, slug="github_someone-else", full_name="someone/else",
                      users=("u1",))
    _seed_runs(env.runs.db_path, [
        ("r1", "u1", f"github:{FULL_NAME}#1", "done", "t", FULL_NAME),
        ("r9", "u1", "github:someone/else#9", "done", "t", "someone/else"),
    ])
    mgr = GroupManager(env.settings)
    g = mgr.create("mixed")
    g.add_repo(f"https://github.com/{FULL_NAME}")
    g.add_repo("someone/else")
    mgr.save(g)

    report = await purge_repo(SLUG, session=FakeSession())

    assert report.errors == []
    assert env.settings.repo_path("github_someone-else").exists()
    assert _count(env.ar.db_path, "auto_review_config") == 1
    assert _count(env.runs.db_path, "review_runs") == 1
    assert mgr.load("mixed").repos == ["someone/else"]


async def test_one_dead_store_does_not_block_the_rest(env, monkeypatch):
    """Purge is what you reach for when a repo is already broken — a Qdrant
    outage must not leave the clone and DB rows behind."""
    _seed_disk(env.settings, SLUG)
    _seed_auto_review(env.ar)

    def boom():
        raise RuntimeError("qdrant down")

    monkeypatch.setattr("src.retrieval.vector_store.get_vector_client", boom)

    report = await purge_repo(SLUG, session=FakeSession())

    assert len(report.errors) == 1 and "qdrant" in report.errors[0]
    assert report.clone_dir_removed
    assert report.auto_review_rows_removed == 2


@pytest.mark.parametrize("identifier", [
    f"https://github.com/{FULL_NAME}",
    f"github:{FULL_NAME}",
    f"git@github.com:{FULL_NAME}.git",
])
async def test_group_membership_matched_in_any_identifier_form(env, identifier):
    """Groups keep whatever the user typed; matching is on the parsed slug."""
    mgr = GroupManager(env.settings)
    g = mgr.create("g")
    g.repos = [identifier]
    mgr.save(g)

    report = await purge_repo(SLUG, session=FakeSession())

    assert report.group_memberships_removed == 1
    assert mgr.load("g").repos == []


async def test_unparseable_group_entry_is_left_alone(env):
    mgr = GroupManager(env.settings)
    g = mgr.create("g")
    g.repos = ["!!! not a repo", f"https://github.com/{FULL_NAME}"]
    mgr.save(g)

    report = await purge_repo(SLUG, session=FakeSession())

    assert report.errors == []
    assert mgr.load("g").repos == ["!!! not a repo"]


async def test_skip_flags(env):
    _seed_disk(env.settings, SLUG)

    report = await purge_repo(
        SLUG, session=FakeSession(), skip_qdrant=True, skip_disk=True,
    )

    assert report.errors == []
    assert report.qdrant_points_deleted == 0
    assert env.qdrant.deleted_with is None
    assert not report.clone_dir_removed
    assert env.settings.repo_path(SLUG).exists()


async def test_never_indexed_repo_is_a_noop(env):
    """Nothing on disk, no rows, no collection — must report zeroes, not raise."""
    env.qdrant._exists = False

    report = await purge_repo(SLUG, session=FakeSession(rowcount=0))

    assert report.errors == []
    assert report.qdrant_points_deleted == 0
    assert not report.clone_dir_removed
    assert report.auto_review_rows_removed == 0
    assert report.review_run_rows_removed == 0
    assert report.group_memberships_removed == 0


async def test_report_dict_has_the_keys_the_ui_reads(env):
    """web/app/(app)/repositories/page.tsx destructures exactly these."""
    report = await purge_repo(SLUG, session=FakeSession())
    d = report.as_dict()

    for key in ("qdrant_points_deleted", "disk_bytes_freed",
                "group_memberships_removed", "errors"):
        assert key in d, key
    assert isinstance(d["errors"], list)
    # CLI reads these off the object
    for attr in ("clone_dir_path", "data_dir_path", "vault_dir_path",
                 "project_repo_links_removed", "auto_review_rows_removed",
                 "review_run_rows_removed", "groups_touched", "elapsed_seconds"):
        assert hasattr(report, attr), attr
