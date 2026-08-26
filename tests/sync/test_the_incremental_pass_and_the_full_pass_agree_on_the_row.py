"""One row, one meaning, whichever pass wrote it.

`repo_index_state` had two candidate writers and only one of them wrote: the
incremental pass in src/sync/incremental.py kept its own session and its own
column semantics, while the full index (src/repos/indexing.py) wrote nothing at
all. Both now go through src/repos/index_state.py. These drive the REAL
`run_index` against a real git checkout so the reconciliation is a property of
the running code and not of a comment:

  * a full rebuild started from here records the same three facts the full
    index path records — sha, time, and that it was a full rebuild;
  * a pass that dies records the error and leaves the last good sha alone,
    exactly as the other writer does;
  * the "HEAD is what we already indexed" shortcut refreshes the timestamp
    but does NOT clear a standing error — no work was done to fix it.

The extractor is stubbed; the git repository, the sha and the database are
real, which is where every bug in this row has come from.
"""

from __future__ import annotations

import subprocess
import types
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa

from src.config import Settings
from src.db.models import RepoIndexState
from src.repos.index_state import read_index_state, record_index_failure
from src.sync.incremental import run_index

SLUG = "github_Acme-Dev-todo-app"


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def _commit(repo: Path, name: str, body: str) -> str:
    (repo / name).write_text(body)
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c", "user.email=test@celmis.local",
        "-c", "user.name=Celmis Test",
        "commit", "-q", "-m", f"add {name}",
    )
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def world(tmp_path, monkeypatch):
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
    monkeypatch.setattr("src.groups.manager.get_settings", lambda: settings)

    repo = settings.repo_path(SLUG)
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    sha = _commit(repo, "a.py", "def a(): ...\n")

    calls: list[Path] = []

    def _fake_index_repo_graph(*, repo_path, repo_slug, src_subdir=None):
        calls.append(repo_path)
        return types.SimpleNamespace(files_processed=9, symbols=42, edges=7)

    monkeypatch.setattr(
        "src.indexing.graph.pipeline.index_repo_graph", _fake_index_repo_graph,
    )

    return types.SimpleNamespace(
        settings=settings, repo=repo, sha=sha, calls=calls, db=db,
    )


def test_a_full_rebuild_from_here_records_the_same_three_facts(world):
    before = datetime.now(UTC)

    result = run_index(SLUG, force_full=True)

    assert result["status"] == "ok"
    state = read_index_state(SLUG)
    assert state.last_indexed_sha == world.sha
    assert before <= state.last_indexed_at <= datetime.now(UTC)
    assert state.last_full_rebuild_at == state.last_indexed_at
    assert state.last_indexed_files == 9
    assert state.last_error is None


def test_a_pass_that_dies_keeps_the_last_good_sha_and_says_what_happened(world):
    run_index(SLUG, force_full=True)
    good = read_index_state(SLUG)

    def _explode(**kwargs):
        raise RuntimeError("tree-sitter died on a.py")

    import src.indexing.graph.pipeline as pipeline
    pipeline.index_repo_graph = _explode
    new_sha = _commit(world.repo, "b.py", "def b(): ...\n")
    assert new_sha != world.sha

    with pytest.raises(RuntimeError):
        run_index(SLUG, force_full=True)

    after = read_index_state(SLUG)
    assert after.last_indexed_sha == good.last_indexed_sha, (
        "a failed pass advanced the sha to a revision that is not in the graph"
    )
    assert after.last_indexed_at == good.last_indexed_at
    assert "tree-sitter died" in after.last_error
    assert after.last_error_at is not None


def test_the_unchanged_shortcut_refreshes_the_time_but_keeps_the_error(world):
    """HEAD is the revision already in the graph, so nothing was indexed — and
    nothing was fixed either. Clearing the error here would claim otherwise."""
    run_index(SLUG, force_full=True)
    record_index_failure(SLUG, "a later attempt died")
    before = read_index_state(SLUG)

    result = run_index(SLUG)

    assert result["status"] == "noop"
    after = read_index_state(SLUG)
    assert after.last_indexed_sha == world.sha
    assert after.last_indexed_at >= before.last_indexed_at
    assert after.last_error == "a later attempt died"


def test_the_recorded_sha_is_what_the_next_pass_diffs_from(world, monkeypatch):
    """The row is not decoration: `run_index` reads it back to choose between a
    rebuild and a diff, and to decide WHICH range to diff. A sha nobody wrote
    means a full rebuild of every repo on every pass, forever.

    Only the per-file delta itself is stubbed here — it reaches the graph store
    and the vector store, neither of which this row's meaning depends on.
    """
    run_index(SLUG, force_full=True)
    assert len(world.calls) == 1

    seen: list[tuple[str, str]] = []

    def _fake_incremental(repo_slug, repo_path, prior_sha, head_sha):
        seen.append((prior_sha, head_sha))
        return {"mode": "incremental", "files_touched": 1}

    monkeypatch.setattr("src.sync.incremental._run_incremental", _fake_incremental)
    new_sha = _commit(world.repo, "b.py", "def b(): ...\n")

    result = run_index(SLUG)

    assert result["mode"] == "incremental", (
        "the second pass rebuilt from scratch — it did not find the sha the "
        "first one recorded"
    )
    assert len(world.calls) == 1, "the full rebuild ran a second time"
    assert seen == [(world.sha, new_sha)], (
        "the diff range did not start at the revision the first pass recorded"
    )
    assert read_index_state(SLUG).last_indexed_sha == new_sha
