"""The full index has to leave a trace saying WHEN and at WHICH revision.

`repo_index_state` existed since migration b5f83a1c9d02 and held zero rows in
production after six successful `index_repo_full` jobs, because the only writer
was the incremental pass. So "is this repo indexed?" was answered everywhere by
`settings.repo_graph_path(slug).exists()` — a file-exists check that cannot
tell an hour-old graph from a March one, cannot name the revision it was built
from, and reads a repo whose indexing has failed six times exactly like a repo
nobody has indexed yet. That is why 161 Martian-bench review runs went out with
"(no graph context)" and no surface could say so.

What these hold down, driving the real `index_repo_sync`:

  * a success records the clone HEAD it indexed, the time, and that it was a
    full rebuild;
  * a second index moves both forward;
  * a failure records the error WITHOUT destroying the last good revision —
    "the graph is from X, and the attempt after it died" is two facts and the
    row holds both;
  * a run that indexed SOMEBODY ELSE'S slug — reachable when the indexer writes
    a different slug and a graph file from an earlier run keeps the existence
    check happy — neither erases the revision this repo's graph was really
    built from nor dates a rebuild that did not happen. A bookkeeping row that
    is more optimistic than the run it describes is the same disease as a
    review that says nothing about a graph it never had;
  * bookkeeping that cannot be written does not fail, and does not hide, an
    index that worked.

The database is a temporary sqlite file rather than a mock: the writers build
their own engine from DATABASE_URL, so faking the session would test the fake.
"""

from __future__ import annotations

import types
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa

from src.api.auto_review import RepoConfig
from src.config import Settings
from src.db.models import RepoIndexState
from src.groups.indexer import GroupIndexResult, _RepoIndexResult
from src.repos.index_state import (
    read_index_state,
    read_index_states,
    record_index_failure,
    record_index_success,
)
from src.repos.indexing import IndexError_, index_repo_sync
from src.sync.clone import SyncResult

SLUG = "github_Acme-Dev-todo-app"
FULL_NAME = "Acme-Dev/todo-app"
SHA_A = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f80911"
SHA_B = "0f1e2d3c4b5a69788796a5b4c3d2e1f009182736"


# ─── the world index_repo_sync runs in ───────────────────────────────


class _Store:
    """The registration store, with exactly one repo in it."""

    def __init__(self, cfg: RepoConfig | None) -> None:
        self._cfg = cfg

    def get_in_workspace(self, workspace_id, repo_slug):
        if self._cfg and self._cfg.repo_slug == repo_slug:
            return self._cfg
        return None

    def get(self, user_id, repo_slug):
        return None


class _Indexer:
    """Stands in for GroupIndexer: writes the graph file, reports one repo.

    Returns the REAL result dataclasses — the sha the product records comes off
    `SyncResult.commit_sha`, so a hand-rolled stand-in would not pin where it
    is read from.
    """

    sha = SHA_A
    files = 37
    raises: BaseException | None = None
    graph_path: Path | None = None
    slug = SLUG

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs

    def index(self):
        if type(self).raises is not None:
            raise type(self).raises
        gp = type(self).graph_path
        if gp is not None:
            gp.parent.mkdir(parents=True, exist_ok=True)
            gp.write_bytes(b"graph")
        return GroupIndexResult(
            group_name="_solo",
            repos_indexed=[
                _RepoIndexResult(
                    slug=type(self).slug,
                    sync=SyncResult(
                        repo_slug=type(self).slug,
                        path=Path("/nowhere"),
                        commit_sha=type(self).sha,
                        changed=True,
                    ),
                    files_processed=type(self).files,
                    files_skipped=0,
                    parse_failures=0,
                    symbols=120,
                    edges_resolved=45,
                    elapsed_seconds=1.0,
                )
            ],
        )


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A temp sqlite `repo_index_state`, a registered repo, a fake indexer."""
    db = tmp_path / "celmis.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    engine = sa.create_engine(f"sqlite:///{db}")
    RepoIndexState.__table__.create(engine)
    engine.dispose()

    settings = Settings(
        gemini_api_key="test-key",
        workspace_dir=tmp_path / "ws",
        vault_dir=tmp_path / "vault",
    )
    settings.ensure_directories()
    monkeypatch.setattr("src.config.get_settings", lambda: settings)

    cfg = RepoConfig(
        user_id="u1", repo_slug=SLUG, provider="github",
        full_name=FULL_NAME, url=f"https://github.com/{FULL_NAME}",
        workspace_id="ws-1",
    )
    monkeypatch.setattr(
        "src.api.auto_review.get_auto_review_store", lambda: _Store(cfg),
    )
    monkeypatch.setattr(
        "src.credentials.resolve_git_credential",
        lambda *a, **k: types.SimpleNamespace(secret="ghp_token", metadata={}),
    )

    _Indexer.sha = SHA_A
    _Indexer.files = 37
    _Indexer.raises = None
    _Indexer.slug = SLUG
    _Indexer.graph_path = settings.repo_graph_path(SLUG)
    monkeypatch.setattr("src.groups.indexer.GroupIndexer", _Indexer)

    return types.SimpleNamespace(settings=settings, cfg=cfg, db=db, tmp=tmp_path)


def _index():
    return index_repo_sync(SLUG, user_id="u1", workspace_id="ws-1")


# ─── a success says when, and at which revision ──────────────────────


def test_a_successful_full_index_records_the_revision_and_the_time(world):
    before = datetime.now(UTC)

    assert _index().repo_slug == SLUG

    state = read_index_state(SLUG)
    assert state is not None, "a successful index left no trace at all"
    assert state.last_indexed_sha == SHA_A
    assert state.short_sha == SHA_A[:8]
    assert state.last_indexed_at is not None
    assert before <= state.last_indexed_at <= datetime.now(UTC)
    assert state.last_full_rebuild_at == state.last_indexed_at
    assert state.last_indexed_files == 37
    assert state.last_error is None
    assert state.failed_since_last_success is False


def test_the_recorded_revision_is_the_one_the_clone_was_at(world):
    """Not "some sha" — the commit_sha of the clone that was walked."""
    _Indexer.sha = SHA_B

    _index()

    assert read_index_state(SLUG).last_indexed_sha == SHA_B


def test_a_second_index_moves_the_revision_and_the_clock_forward(world):
    _index()
    first = read_index_state(SLUG)

    _Indexer.sha = SHA_B
    _Indexer.files = 4
    _index()
    second = read_index_state(SLUG)

    assert first.last_indexed_sha == SHA_A
    assert second.last_indexed_sha == SHA_B
    assert second.last_indexed_at >= first.last_indexed_at
    assert second.last_full_rebuild_at >= first.last_full_rebuild_at
    assert second.last_indexed_files == 4


# ─── a failure says so, and keeps the last good revision ─────────────


def test_a_failing_index_records_the_error_without_destroying_the_good_sha(world):
    _index()
    good = read_index_state(SLUG)

    _Indexer.raises = RuntimeError("dangling symlink packages/prisma/.env")
    with pytest.raises(IndexError_):
        _index()

    after = read_index_state(SLUG)
    assert after.last_indexed_sha == SHA_A, (
        "the failed attempt overwrote the revision that is actually on disk"
    )
    assert after.last_indexed_at == good.last_indexed_at
    assert after.last_full_rebuild_at == good.last_full_rebuild_at
    assert "dangling symlink" in after.last_error
    assert after.failed_since_last_success is True


def test_the_failure_carries_the_time_it_happened(world):
    before = datetime.now(UTC)
    _Indexer.raises = RuntimeError("boom")

    with pytest.raises(IndexError_):
        _index()

    state = read_index_state(SLUG)
    assert state.last_error_at is not None
    assert before <= state.last_error_at <= datetime.now(UTC)


def test_a_repo_that_never_indexed_still_says_the_attempt_died(world):
    """cal.diy failed six times and left nothing but a dead queue row — an
    un-indexed repo and a repo whose index keeps dying must not read alike."""
    _Indexer.raises = RuntimeError("clone refused")

    with pytest.raises(IndexError_):
        _index()

    state = read_index_state(SLUG)
    assert state is not None, "six failures in a row and still nothing recorded"
    assert state.last_indexed_sha is None
    assert state.last_indexed_at is None
    assert "clone refused" in state.last_error


def test_a_later_success_retires_the_failure(world):
    _Indexer.raises = RuntimeError("boom")
    with pytest.raises(IndexError_):
        _index()

    _Indexer.raises = None
    _Indexer.sha = SHA_B
    _index()

    state = read_index_state(SLUG)
    assert state.last_error is None
    assert state.last_error_at is None
    assert state.last_indexed_sha == SHA_B


def test_an_indexer_that_wrote_another_slug_records_no_revision(world):
    """The graph file check passes off an EARLIER run's file, so the call
    returns success while the sha in hand belongs to a different repository.
    Recording it would put another repo's revision on this row; recording the
    run without one at least dates the graph honestly."""
    _Indexer.slug = "github_someone-else"

    _index()

    state = read_index_state(SLUG)
    assert state.last_indexed_sha is None
    assert state.last_indexed_at is not None
    assert state.last_indexed_files == 0


def test_a_repo_this_workspace_never_registered_gets_no_row(world):
    """An orphan row for an unregistered slug is one no purge is asked to
    remove."""
    with pytest.raises(IndexError_):
        index_repo_sync("github_someone-else", user_id="u1", workspace_id="ws-1")

    assert read_index_state("github_someone-else") is None


# ─── bookkeeping never fails, and never hides, the real work ─────────


def _break_the_writes(monkeypatch):
    def _explode():
        raise RuntimeError("connection pool exhausted")

    monkeypatch.setattr("src.repos.index_state._session", _explode)


def test_a_bookkeeping_write_that_raises_does_not_fail_the_index(world, monkeypatch):
    """An index that succeeded and then could not write a row has succeeded."""
    _break_the_writes(monkeypatch)

    result = _index()

    assert result.repo_slug == SLUG
    assert result.symbols == 120


def test_an_index_on_a_worker_with_no_database_still_succeeds(world, monkeypatch):
    """DATABASE_URL unset is a real worker configuration — `get_database_url`
    raises ValueError, and that must not become the index's problem."""
    monkeypatch.delenv("DATABASE_URL", raising=False)

    assert _index().repo_slug == SLUG


def test_a_bookkeeping_write_that_raises_does_not_hide_the_index_error(
    world, monkeypatch,
):
    """The person gets the indexer's message, not the bookkeeper's."""
    _Indexer.raises = RuntimeError("dangling symlink packages/prisma/.env")
    _break_the_writes(monkeypatch)

    with pytest.raises(IndexError_) as excinfo:
        _index()

    assert "dangling symlink" in str(excinfo.value)
    assert "connection pool" not in str(excinfo.value)


# ─── reading it back ─────────────────────────────────────────────────


def test_the_reader_knows_nothing_about_a_repo_that_never_indexed(world):
    assert read_index_state("github_never-touched") is None


def test_the_reader_survives_a_database_it_cannot_reach(world, monkeypatch):
    """The repositories list renders without a database today and must keep
    doing so — losing the freshness column beats a 500."""
    monkeypatch.delenv("DATABASE_URL", raising=False)

    assert read_index_state(SLUG) is None
    assert read_index_states([SLUG]) == {}


def test_the_batch_reader_answers_for_the_repos_it_was_asked_about(world):
    """`repo-z` has a row and was not asked for: the list screen renders one
    workspace, and a batch that answers with every tenant's rows is the same
    leak the vector reads were scoped to close."""
    record_index_success("repo-a", sha=SHA_A, files=3, full_rebuild=True)
    record_index_failure("repo-b", "went bang")
    record_index_success("repo-z", sha=SHA_B, files=1, full_rebuild=True)

    states = read_index_states(["repo-a", "repo-b", "repo-c"])

    assert set(states) == {"repo-a", "repo-b"}, (
        "a slug with no row must be absent, and a slug nobody asked about must "
        "not appear"
    )
    assert states["repo-a"].last_indexed_sha == SHA_A
    assert states["repo-b"].last_error == "went bang"
    assert states["repo-b"].last_indexed_sha is None


def test_the_batch_reader_asks_nothing_when_given_nothing(world, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    assert read_index_states([]) == {}


def test_an_error_written_without_the_timestamp_prefix_still_reads_back(world):
    """Rows predating the encoding, or written by hand in psql, keep their
    whole text rather than losing it to a failed parse."""
    engine = sa.create_engine(f"sqlite:///{world.db}")
    with engine.begin() as conn:
        conn.execute(sa.insert(RepoIndexState.__table__).values(
            repo_slug="legacy", last_error="plain old message", last_incremental_files=0,
        ))
    engine.dispose()

    state = read_index_state("legacy")
    assert state.last_error == "plain old message"
    assert state.last_error_at is None


# ─── a run that indexed nothing here does not claim it rebuilt it ────


def _index_under_another_slug():
    """The branch: the indexer reports a different slug, our graph file exists."""
    _Indexer.slug = "github_Acme-Dev-somebody-else"
    return _index()


def test_a_run_that_indexed_another_slug_keeps_the_last_good_revision(world):
    """The graph on disk is still the one built from SHA_A. Forgetting which
    revision that was makes the row LESS true than it was before the run, and
    replaces a fact with an absence that reads as "never recorded"."""
    assert _index().repo_slug == SLUG
    assert read_index_state(SLUG).last_indexed_sha == SHA_A

    _index_under_another_slug()

    assert read_index_state(SLUG).last_indexed_sha == SHA_A, (
        "a run that indexed somebody else's slug erased the revision this "
        "repo's graph was actually built from"
    )


def test_a_run_that_indexed_another_slug_does_not_date_a_rebuild(world):
    """`last_full_rebuild_at` answers "when was this graph built from scratch".
    Moving it forward for a run that wrote no graph HERE dates a rebuild that
    never happened."""
    assert _index().repo_slug == SLUG
    first = read_index_state(SLUG).last_full_rebuild_at
    assert first is not None

    _index_under_another_slug()

    assert read_index_state(SLUG).last_full_rebuild_at == first


def test_a_second_real_rebuild_still_moves_both_forward(world):
    """The guard must cost the honest path nothing."""
    assert _index().repo_slug == SLUG
    first = read_index_state(SLUG)

    _Indexer.sha = SHA_B
    assert _index().repo_slug == SLUG

    second = read_index_state(SLUG)
    assert second.last_indexed_sha == SHA_B
    assert second.last_full_rebuild_at >= first.last_full_rebuild_at


# ─── the recorder itself: None means "left alone", not "erased" ──────


def test_a_success_without_a_revision_leaves_the_recorded_one_alone(world):
    record_index_success(SLUG, sha=SHA_A, files=3, full_rebuild=True)
    record_index_success(SLUG, sha=None, files=0, full_rebuild=False)

    assert read_index_state(SLUG).last_indexed_sha == SHA_A


def test_a_success_with_a_revision_still_replaces_the_old_one(world):
    record_index_success(SLUG, sha=SHA_A, files=3, full_rebuild=True)
    record_index_success(SLUG, sha=SHA_B, files=4, full_rebuild=True)

    assert read_index_state(SLUG).last_indexed_sha == SHA_B


def test_a_first_ever_success_without_a_revision_still_records_the_run(world):
    """None must mean "left alone", never "refused": a repo with no row yet
    still gets one, so the run does not become invisible."""
    record_index_success(SLUG, sha=None, files=0, full_rebuild=True)

    state = read_index_state(SLUG)
    assert state is not None
    assert state.last_indexed_sha is None
    assert state.last_indexed_at is not None


@pytest.mark.parametrize("full_rebuild", [True, False])
def test_the_rebuild_stamp_follows_its_argument(world, full_rebuild):
    record_index_success(SLUG, sha=SHA_A, files=1, full_rebuild=full_rebuild)

    assert (read_index_state(SLUG).last_full_rebuild_at is not None) is full_rebuild
