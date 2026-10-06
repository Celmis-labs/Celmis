"""Two facts the developer profile prints on every answer come from the index row.

* WHICH BRANCH the indexed revision belongs to. `idx: slug develop@a1b2c3d4`
  is only honest if the branch was recorded alongside the sha.
* A repository that moved further than the shallow clone reaches no longer
  holds the revision it was indexed at. `git diff prior..head` then failed on
  every push and the repository stayed stale exactly when it was busiest. The
  pass now rebuilds from the checkout instead.
"""

from __future__ import annotations

import subprocess
import types
from pathlib import Path

import pytest
import sqlalchemy as sa

from src.config import Settings
from src.db.models import RepoIndexState
from src.repos.index_state import read_index_state, record_index_success
from src.sync.incremental import _commit_present, run_index

SLUG = "github_Acme-Dev-todo-app"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, check=True).stdout.strip()


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(f"def {name[0]}(): ...\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@example.com", "-c", "user.name=T",
         "-c", "commit.gpgsign=false", "commit", "-q", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def world(tmp_path, monkeypatch):
    db = tmp_path / "celmis.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    engine = sa.create_engine(f"sqlite:///{db}")
    RepoIndexState.__table__.create(engine)
    engine.dispose()
    settings = Settings(gemini_api_key="test-key", workspace_dir=tmp_path / "ws",
                        vault_dir=tmp_path / "vault")
    settings.ensure_directories()
    monkeypatch.setattr("src.config.get_settings", lambda: settings)
    monkeypatch.setattr("src.groups.manager.get_settings", lambda: settings)
    repo = settings.repo_path(SLUG)
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "develop")
    sha = _commit(repo, "a.py")
    full_calls: list[Path] = []

    def fake_full(*, repo_path, repo_slug, src_subdir=None):
        full_calls.append(repo_path)
        return types.SimpleNamespace(files_processed=1, symbols=1, edges=0)

    monkeypatch.setattr("src.indexing.graph.pipeline.index_repo_graph", fake_full)
    return types.SimpleNamespace(repo=repo, sha=sha, full_calls=full_calls)


def test_a_full_index_records_the_branch_the_clone_stands_on(world):
    run_index(SLUG, force_full=True)
    state = read_index_state(SLUG)
    assert state.indexed_branch == "develop" and state.last_indexed_sha == world.sha


def test_a_later_success_without_a_branch_does_not_blank_the_recorded_one(world):
    record_index_success(SLUG, sha=world.sha, files=1, full_rebuild=True, branch="develop")
    record_index_success(SLUG, sha=world.sha, files=1, full_rebuild=False, branch=None)
    assert read_index_state(SLUG).indexed_branch == "develop"


def test_a_new_revision_on_another_branch_replaces_the_recorded_branch(world):
    record_index_success(SLUG, sha=world.sha, files=1, full_rebuild=True, branch="develop")
    record_index_success(SLUG, sha="e" * 40, files=1, full_rebuild=True, branch="release")
    state = read_index_state(SLUG)
    assert (state.indexed_branch, state.last_indexed_sha) == ("release", "e" * 40)


def test_a_commit_the_clone_still_has_is_present_and_a_dropped_one_is_not(world):
    assert _commit_present(world.repo, world.sha) is True
    assert _commit_present(world.repo, "1" * 40) is False


def test_an_indexed_revision_missing_from_a_shallow_clone_triggers_a_full_rebuild(world):
    record_index_success(SLUG, sha="1" * 40, files=1, full_rebuild=True, branch="develop")
    new_sha = _commit(world.repo, "b.py")
    result = run_index(SLUG)
    assert result["status"] == "ok" and result["mode"] == "full"
    assert len(world.full_calls) == 1
    assert read_index_state(SLUG).last_indexed_sha == new_sha


def test_a_present_prior_revision_still_takes_the_incremental_path(world, monkeypatch):
    run_index(SLUG, force_full=True)
    seen: list[tuple[str, str]] = []

    def fake_incremental(slug, path, prior, head):
        seen.append((prior, head))
        return {"mode": "incremental", "files_touched": 1}

    monkeypatch.setattr("src.sync.incremental._run_incremental", fake_incremental)
    new_sha = _commit(world.repo, "c.py")
    run_index(SLUG)
    assert seen == [(world.sha, new_sha)] and len(world.full_calls) == 1
